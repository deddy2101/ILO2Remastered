FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ilo2/ ilo2/
COPY web/ web/
COPY webmain.py .

# Config (ILO_HOST/USER/PASSWORD, optional WEBAPP_USER/PASSWORD) comes in
# via environment variables at `docker run`/compose time -- see .env.example
# -- not baked into the image, so the same image works for any iLO2 and
# never bundles credentials into a layer.
EXPOSE 8080 8765

CMD ["python3", "webmain.py"]
