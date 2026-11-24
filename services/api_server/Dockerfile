FROM aegis-base

COPY services/ /app/services/
COPY scripts/ /app/scripts/

EXPOSE 8080

CMD ["python", "-m", "services.api_server.main"]
