FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples

RUN pip install --no-cache-dir .

# The offline demo: one synthetic location, every decision named. The build
# resolves the backend and pydantic from PyPI; the run makes no network call
# and needs no credential, because the workspace in the demo is the
# deterministic fake this package ships.
CMD ["python", "-m", "examples.bridge_demo"]
