#!/bin/bash
uvicorn Server.api:app --host 0.0.0.0 --port 8000 &
streamlit run Client/web.py