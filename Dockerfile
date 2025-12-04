
FROM python:3.10-slim


RUN apt-get update && apt-get install -y \
    gdal-bin \
    libgdal-dev \
    libproj-dev \
    libgeos-dev \
    libhdf5-dev \
    libnetcdf-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


COPY . .


EXPOSE 8000

CMD ["uvicorn", "api_all:app", "--host", "0.0.0.0", "--port", "8000"]
