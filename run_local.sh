#!/bin/bash
uvicorn server.api:app  --host 0.0.0.0 --port 8000 &
streamlit run gui/web.py