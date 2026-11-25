from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException

from src.config.resources import engine
from src.controllers.access import identity, operator
from src.controllers.workbench import new_transfer
from src.schemas.transfers import (
    DossierEntry,
    PointCheckInput,
    PointCheckRow,
    TransferInput,
    TransferOrderEntry,
    TransferVerification,
)
from src.services import transfers

router = APIRouter(prefix="/transfers", tags=["Transfers"])


@router.get("/dossiers", response_model=list[DossierEntry], dependencies=[Depends(identity)])
def get_dossiers():
    with engine.connect() as db:
        return transfers.list_dossiers(db)


@router.post("/orders", response_model=TransferOrderEntry, status_code=201)
def create_transfer(form: TransferInput, user=Depends(operator)):
    return new_transfer(form, user)


@router.get("/orders", response_model=list[TransferOrderEntry], dependencies=[Depends(identity)])
def get_orders():
    with engine.connect() as db:
        return transfers.list_orders(db)


@router.get("/orders/{order_id}", response_model=TransferOrderEntry, dependencies=[Depends(identity)])
def get_order(order_id: int):
    with engine.connect() as db:
        return transfers.get_order(db, order_id)


@router.get("/orders/{order_id}/verification",
            response_model=TransferVerification, dependencies=[Depends(identity)])
def verify(order_id: int, review_date: str):
    try:
        day = date_type.fromisoformat(review_date)
    except ValueError as exc:
        raise HTTPException(422, "巡检日须用 YYYY-MM-DD") from exc
    with engine.connect() as db:
        return transfers.verify_order(db, order_id, day)


@router.post("/point-check", response_model=list[PointCheckRow], dependencies=[Depends(identity)])
def point_check(form: PointCheckInput):
    with engine.connect() as db:
        return transfers.point_check(db, form.probes, form.review_date)
