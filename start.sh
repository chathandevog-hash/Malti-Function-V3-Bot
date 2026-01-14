#!/bin/bash
python3 bot.py &
gunicorn web:app --bind 0.0.0.0:$PORT
