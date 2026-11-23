# Airflow 공식 이미지에 프로젝트 코드가 쓰는 패키지만 더한다.
# requests, pandas, psycopg2는 공식 이미지에 이미 들어 있으므로 python-dotenv만 추가한다.
# apache-airflow를 같은 버전으로 함께 적어야 pip가 Airflow를 다른 버전으로 바꾸지 않는다.
FROM apache/airflow:3.3.2-python3.12

RUN pip install --no-cache-dir "apache-airflow==3.3.2" python-dotenv
