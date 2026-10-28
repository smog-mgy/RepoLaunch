from fastapi import FastAPI

app = FastAPI(title="RepoLaunch Hello Web")

@app.get("/")
def home():
    return {"message": "Hello from RepoLaunch", "project": "hello-web"}

@app.get("/health")
def health():
    return {"status": "ok"}
