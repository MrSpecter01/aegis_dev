FROM aegis-base

COPY services/ /app/services/

EXPOSE 9008

CMD ["python", "-m", "services.mcp.topology.server"]
