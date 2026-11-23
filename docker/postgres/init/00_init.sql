-- PostgreSQL 컨테이너를 처음 띄울 때(./data/postgres가 비어 있을 때) 한 번만 실행된다.
-- POSTGRES_DB(dartkg)에 접속한 상태로 실행된다.

-- Airflow 메타데이터용 DB. 프로젝트 데이터와 섞이지 않게 분리한다.
CREATE DATABASE airflow;

-- Phase 3 임베딩 검색용 확장. 미리 켜 둔다.
CREATE EXTENSION IF NOT EXISTS vector;
