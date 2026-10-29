FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GRIDIO_DATA_DIR=/data
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
COPY --chown=app:app gridio_service /app/gridio_service
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "gridio_service"]
CMD ["serve"]
