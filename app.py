from flask import Flask, Markup, request, redirect, render_template, jsonify, make_response
from flask_cors import CORS
import requests
from datetime import date
import time
import os
import json
from fred import Fred
import pandas as pd
import numpy as np
import io
import csv

#INIT THE CLIENT
API_KEY = os.environ.get('FRED_API_KEY')
fr = Fred(api_key=API_KEY,response_type='json')

#creates instance of app
app = Flask(__name__)
CORS(app)
app.config.from_object(__name__)

@app.route("/")
def health():
    return 'OK'    

@app.route("/fredsearch")
def fredsearch():

    searchKey = request.args.get('searchKey')

    if searchKey == None:
        return "Error: no search terms provided"

    print("recieved search term: " + str(searchKey))

    params = {'limit':50,}
    res = fr.series.search(searchKey,params=params)
    observations = json.dumps(json.loads(res))
    SEARCH_RES_DF = pd.read_json(observations)
    SEARCH_RES_DF_NEW = SEARCH_RES_DF['seriess']
    print("successfully fetched the search results. returning repsonse now")
    # Convert back to JSON
    return(SEARCH_RES_DF_NEW.to_json())


@app.route('/test_download')
def testing_download():
    si = io.StringIO()
    cw = csv.writer(si)
    csvList = """"REVIEW_DATE","AUTHOR","ISBN","DISCOUNTED_PRICE"
        "1985/01/21","Douglas Adams",0345391802,5.95
        "1990/01/12","Douglas Hofstadter",0465026567,9.95
        "1998/07/15","Timothy ""The Parser"" Campbell",0968411304,18.99
        "1999/12/03","Richard Friedman",0060630353,5.95
        "2004/10/04","Randel Helms",0879755725,4.50"""
    cw.writerows(csvList)
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=export.csv"
    output.headers["Content-type"] = "text/csv"
    return output


#receive payload (series names + fill prefs, target frequency)
@app.route("/retrievedata", methods=['GET', 'POST'])
def data_request_hof():

    #read in the target frequency    
    target_output_frequency = request.json['target_frequency']
    
    #expect data_series to be a list of objects
    requested_series_identifier_list  = request.json['requested_series_identifier_list']

    if target_output_frequency == None:
        return "Error: must specify a target frequency", 404    
    if requested_series_identifier_list == None:
        return "Error: Must provide at least 1 requested data series identifier", 404
    
    print('requested output frequency', target_output_frequency)
    print('requested series', requested_series_identifier_list)

    #define list of objects to send to the next step
    outgoing_dataseries_list = []
    earliest_date = None
    latest_date = None

    #looooooooooooop
    i = 0
    for requested_series in requested_series_identifier_list:

        #2 params on each series object
        series_identifier = requested_series['series_identifier']
        fill_methodology = requested_series['fill_methodology']

        print("iteration number: ", i, " with identifier: ", series_identifier," and fill methodology: ", fill_methodology)

        #define outgoing data  #TODO: future state hold on to the name and description from the original search result
        detailed_series_data = {
            'series_name': 'placeholder',
            'series_identifier': series_identifier,
            'series_description': 'placeholder',
            'series_frequency': 'placeholder',
            'series_raw_api_response': 'placeholder',
            'series_raw_observations': 'placeholder',
            'series_dataframed': 'placeholder',
            'series_fill_methodology': fill_methodology
            }

        #get the raw data from FRED
        print("now querying FRED for raw data")
        detailed_series_data['series_raw_api_response'] = retrieve_raw_fred_data(series_identifier)
        print("query of fred data successful")
        detailed_series_data['series_raw_observations'] = detailed_series_data['series_raw_api_response']['observations']
        print("now querying FRED for descriptive info")
        detailed_series_data['series_name'] = retrieve_series_name(series_identifier)
        detailed_series_data['series_frequency'] = retrieve_series_frequency(series_identifier)
        

        # convert to a dataframe and clean it up
        print("shifting raw data into dataframe")
        intermediate_state_dataframe = object_2_dataframe(detailed_series_data['series_name'], detailed_series_data['series_raw_observations'])
        print("cleaning dataframe")
        detailed_series_data['series_dataframed'] = df_cleanup(intermediate_state_dataframe, ['realtime_start','realtime_end'])
        print("clean data head", detailed_series_data['series_dataframed'].head())
        #calc min and max date here (looking across all the dataframes)
        print("checking local max/min vs. global")
        local_min  = detailed_series_data['series_dataframed'].index.min()
        local_max  = detailed_series_data['series_dataframed'].index.max()
        
        if earliest_date == None:
            earliest_date = local_min

        elif local_min < earliest_date:
            earliest_date = local_min

        if latest_date == None:
            latest_date = local_max

        elif local_max > latest_date:
            latest_date = local_max

        #add to list of objects
        print("adding resulting df to main object")
        outgoing_dataseries_list.append(detailed_series_data)
        i+=1
        
    #create the overarching earliest to latest timerange
    print("setting up main dataframe")
    main_frame = pd.DataFrame(np.nan, index=pd.date_range(earliest_date, latest_date, freq=target_output_frequency), columns=['NaNs'])
    main_frame.drop(axis=1, columns=['NaNs'], inplace=True)

    # shift every series over to the new frame (expect lots of NaNs)
    print("beginning loop to join dataframes into main")
    for dataseries_object in outgoing_dataseries_list:
        #resample & join the frame into main
        #TODO: figure out how to fill the NaNs that are created when the date range extends before or after the dataset
        print("now backfilling series id: ",dataseries_object['series_identifier']," and merging into the main dataframe")
        resampled_data = resample_hof(dataseries_object, target_output_frequency)
        print(resampled_data)
        main_frame = main_frame.join(resampled_data)
        print("successfully backfilled + merged series id: ",dataseries_object['series_identifier'])


    #cull the dataframe to only the requested frequency- not fully needed now that we create the daterange using the target output frequency
    main_frame = period_end_dataframe(main_frame, target_output_frequency)

    # return the resulting main dataframe as json to the UI
    main_frame.index = main_frame.index.strftime('%Y/%m/%d') 

    print("main dataframe completed. returning the result")
    print("merged", main_frame.head())

    # #return CSV to the UI
    # #https://stackoverflow.com/questions/26997679/writing-a-csv-from-flask-framework
    # si = io.StringIO()
    # cw = csv.writer(si)
    # cw.writerows(main_frame.to_csv())
    # output = make_response(si.getvalue())
    # output.headers["Content-Disposition"] = "attachment; filename=export.csv"
    # output.headers["Content-type"] = "text/csv"
    # print("successfully created csv. returning to caller")
    # return output

    # Convert the DataFrame to CSV format
    csv = main_frame.to_csv(index=True)
    # Create a Flask response with the CSV data
    response = make_response(csv)
    # Set the response headers to suggest a CSV file download
    response.headers['Content-Disposition'] = 'attachment; filename=data.csv'
    response.headers['Content-Type'] = 'text/csv'
    return response

    

def period_end_dataframe(original_dataframe, target_output_frequency):

    #https://pandas.pydata.org/docs/reference/api/pandas.Series.dt.is_month_end.html
    #Examples
    #This method is available on Series with datetime values under the .dt accessor, and directly on DatetimeIndex.
    
    if target_output_frequency == 'D':
        return original_dataframe
    elif target_output_frequency == 'M':
        return(original_dataframe[original_dataframe.index.is_month_end])
    elif target_output_frequency == 'Q':
        return(original_dataframe[original_dataframe.index.is_quarter_end])
    elif target_output_frequency == 'Y':
        return(original_dataframe[original_dataframe.index.is_year_end])
    else:
        return SyntaxError("Missing target output frequency")
    

def object_2_dataframe(title, incoming_object):
    d_frame = pd.read_json(json.dumps(incoming_object))
    d_frame.columns = ['realtime_start', 'realtime_end', 'date', title]

    # replace invalid chars in the data series
    d_frame.replace('-', np.nan)
    d_frame.replace('.', np.nan)

    #corner case where fred sends us a "." as the first entry
    if d_frame[title][0] == ".":
        d_frame[title][0] = np.nan

    #check if data came in as a objects, if so, convert it
    if d_frame[title].dtype not in ['float64', 'int64']:
        d_frame[title] = d_frame[title].astype(str)
        d_frame[title] = d_frame[title].astype(float)

    return d_frame
    
def df_cleanup(dframe, columns_to_remove=None):
    if columns_to_remove is not None:
        dframe.drop(axis=1, columns=columns_to_remove, inplace=True)
    
    #convert date strings to proper datetimes
    dframe['date'] = pd.to_datetime(dframe['date'])
    dframe.set_index('date', inplace=True)
    #cleaner index in YYMMDD format
    #dframe.index = dframe.index.strftime('%Y/%m/%d') 
    return dframe

def retrieve_raw_fred_data(series_identifier):
    # call Fred again....
    #print(fr.category.children(97))
    return json.loads(fr.series.observations(series_identifier))

#TODO: merge these 2 functions into 1 that accepts different characteristic names
def retrieve_series_name(series_identifier):
    return json.loads(fr.series.details(series_identifier))['seriess'][0]['title']

def retrieve_series_frequency(series_identifier):
    return json.loads(fr.series.details(series_identifier))['seriess'][0]['frequency_short']

def resample_hof(incoming_data_object, target_output_frequency):
    series_fill_methodology = incoming_data_object['series_fill_methodology']

    if series_fill_methodology == "fill":
        resampled_dataset = transform_fill(incoming_data_object['series_dataframed'], target_output_frequency)
    elif series_fill_methodology == "prorate":
        resampled_dataset = transform_prorate(incoming_data_object['series_dataframed'], target_output_frequency, incoming_data_object['series_frequency'])
    elif series_fill_methodology == "interpolate":
        resampled_dataset = transform_interpolate(incoming_data_object['series_dataframed'], target_output_frequency)
    elif series_fill_methodology == "average":
        resampled_dataset = transform_average(incoming_data_object['series_dataframed'], target_output_frequency)
    elif series_fill_methodology == "sum":
        resampled_dataset = transform_sum(incoming_data_object['series_dataframed'], target_output_frequency)
    else:
        resampled_dataset = incoming_data_object

    return resampled_dataset

##### Noah OG code
# fill
def transform_fill(series, transform_target):
    #note that bfill pulls the future value back in time. ffill pushes an old observation forward.
    outputSeries = series.resample(transform_target).ffill()
    return outputSeries

# prorate
def transform_prorate(series, transform_target, series_base_freq):
    # Resample data to target frequency
    newDF = series
    if series_base_freq == "M":
        newDF["newdate"] = newDF.index + pd.offsets.MonthEnd(0)
        newDF["prevdate"] = newDF.newdate.shift(1)
        newDF.iloc[0, newDF.columns.get_loc("prevdate")] = newDF.iloc[
            0, newDF.columns.get_loc("newdate")
        ] + pd.offsets.MonthEnd(-1)
    elif series_base_freq == "Q":
        newDF["newdate"] = newDF.index + pd.offsets.QuarterEnd(0)
        newDF["prevdate"] = newDF.newdate.shift(1)
        newDF.iloc[0, newDF.columns.get_loc("prevdate")] = newDF.iloc[
            0, newDF.columns.get_loc("newdate")
        ] + pd.offsets.QuarterEnd(-1)
    elif series_base_freq == "A":
        newDF["newdate"] = newDF.index + pd.offsets.YearEnd(0)
        newDF["prevdate"] = newDF.newdate.shift(1)
        newDF.iloc[0, newDF.columns.get_loc("prevdate")] = newDF.iloc[
            0, newDF.columns.get_loc("newdate")
        ] + pd.offsets.YearEnd(-1)
    else:  # Assume week
        newDF["newdate"] = newDF.index
        newDF["prevdate"] = newDF.newdate.shift(1)
        newDF.iloc[0, newDF.columns.get_loc("prevdate")] = newDF.iloc[
            0, newDF.columns.get_loc("newdate")
        ] + pd.offsets.Week(-1)

    newDF["divisor"] = (newDF["newdate"] - newDF["prevdate"]).dt.days
    outputSeries = newDF.resample(transform_target).ffill()
    outputSeries["oldVal"] = pd.to_numeric(outputSeries.iloc[:, 0], errors="coerce")

    outputSeries.iloc[:, 0] = outputSeries["oldVal"] / outputSeries["divisor"]
    outputSeries.drop(
        axis=1, columns=["newdate", "divisor", "oldVal", "prevdate"], inplace=True
    )
    return outputSeries


# interpolate
def transform_interpolate(series, transform_target):
    outputSeries = series.resample(transform_target).interpolate()
    # Currently using linear interpolation, but may want to consider using a spline - good stats question
    return outputSeries


# average
def transform_average(series, transform_target):
    outputSeries = series.resample(transform_target).mean()
    return outputSeries


# sum
def transform_sum(series, transform_target):
    outputSeries = series.resample(transform_target).sum()
    return outputSeries


#instantiate app
if __name__ == "__main__":
    app.run(debug=True, port=3000)