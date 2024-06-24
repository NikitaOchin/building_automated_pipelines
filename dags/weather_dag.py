import json

from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.providers.http.operators.http import SimpleHttpOperator
from airflow.providers.http.sensors.http import HttpSensor
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.sqlite.operators.sqlite import SqliteOperator
from airflow.utils.dates import days_ago
from airflow.utils.task_group import TaskGroup


cities = {
         "Lviv":      (49.8383, 24.023),
         "Kyiv":      (50.4501, 30.5234),
         "Odesa":     (46.482952, 30.71248),
         "Kharkiv":   (49.988358, 36.232845),
         "Zhmerynka": (49.036872, 28.121135)
         }

def _process_weather(ti, **kwargs):
    info = ti.xcom_pull(f"processing_tasks.extract_data_{kwargs['city'].lower()}")
    timestamp = info["data"][0]["dt"]
    temp = info["data"][0]["temp"]
    humidity = info["data"][0]["humidity"]
    clouds = info["data"][0]["clouds"]
    wind_speed = info["data"][0]["wind_speed"]
    return timestamp, temp, humidity, clouds, wind_speed


def get_execution_date_timestamp_function(**kwargs):
    execution_date = kwargs['execution_date']
    timestamp = int(execution_date.timestamp())
    print(f"Execution date timestamp: {timestamp}")
    return timestamp


with DAG(dag_id="weather_dag", schedule_interval="@daily", start_date=days_ago(2)) as dag:

    check_api = HttpSensor(
        task_id="check_api",
        http_conn_id="weather_conn",
        endpoint="data/3.0/onecall",
        request_params={"appid": Variable.get("WEATHER_API_KEY"), "lat": cities["Lviv"][0], "lon": cities["Lviv"][1]},
    )

    sql_template = """CREATE TABLE IF NOT EXISTS public.measures
			(
    			timestamp  timestamp,
    			temp       float4,
                        humidity   float4,
                        clouds     float4,
                        wind_speed float4,
    			city       varchar
		 );"""
    create_table_if_not_exists = PostgresOperator(
                task_id=f"create_table_if_not_exists",
                postgres_conn_id="ps_db_conn",
                sql=sql_template
            )


    get_execution_date_timestamp = PythonOperator(
        task_id='get_execution_date_timestamp',
        provide_context=True,
        python_callable=get_execution_date_timestamp_function,
        dag=dag,
    )

    with TaskGroup("processing_tasks") as processing_task:
        for city in cities:
            extract_data = SimpleHttpOperator(
                task_id=f"extract_data_{city.lower()}",
                http_conn_id="weather_conn",
                endpoint="data/3.0/onecall/timemachine",
                data={"appid": Variable.get("WEATHER_API_KEY"), "lat": f"{cities[city][0]}", "lon": f"{cities[city][1]}", "dt":"{{ti.xcom_pull(task_ids='get_execution_date_timestamp')}}"},
                method="GET",
                response_filter=lambda x: json.loads(x.text),
                log_response=True,
            )

            process_data = PythonOperator(
                task_id=f"process_data_{city.lower()}", python_callable=_process_weather, op_kwargs={"city": city},
            )

            sql_template = """
            INSERT INTO public.measures (timestamp, temp, humidity, clouds, wind_speed, city)
            VALUES (to_timestamp({{ti.xcom_pull(task_ids='processing_tasks.process_data_city')[0]}}), 
                   {{ti.xcom_pull(task_ids='processing_tasks.process_data_city')[1]}},
                   {{ti.xcom_pull(task_ids='processing_tasks.process_data_city')[2]}},
                   {{ti.xcom_pull(task_ids='processing_tasks.process_data_city')[3]}},
                   {{ti.xcom_pull(task_ids='processing_tasks.process_data_city')[4]}},
                   '_City'
            );""".replace('_city', f"_{city.lower()}").replace('_City', city)
            inject_data = PostgresOperator(
                task_id=f"inject_data_{city.lower()}",
                postgres_conn_id="ps_db_conn",
                sql=sql_template
            )
            extract_data >> process_data >> inject_data
    check_api >> create_table_if_not_exists >> get_execution_date_timestamp >> processing_task
