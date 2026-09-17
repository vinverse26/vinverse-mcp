FROM python:3.11-slim

WORKDIR /app

# git is required by deploy_tool.py's git_commit_files() -- it clones,
# commits, and pushes to target repos directly rather than using GitHub's
# Contents API, which some accounts get blocked from writing to.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py deploy_tool.py storage.py .

EXPOSE 8001

CMD ["python", "server.py"]
