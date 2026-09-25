FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y nodejs npm build-essential
COPY . .
RUN pip install -r requirements.txt
EXPOSE 3000 8000
CMD python main.py && reflex run
