import logging
from fastapi import FastAPI
from mock_api.routes import benches, reservations

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Mock TestBench Reservation API", version="1.0.0", docs_url="/docs")
app.include_router(benches.router)
app.include_router(reservations.router)


@app.get("/health", tags=["dev"])
def health():
    from mock_api import fake_db
    return {"status": "ok", "benches": len(fake_db.benches),
            "users": len(fake_db.users), "reservations": len(fake_db.reservations)}
