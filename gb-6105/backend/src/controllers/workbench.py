from redis.exceptions import RedisError
from src.config.resources import cache, engine
from src.services import cases, runs, reviews, transfers


def new_case(data, user):
    with engine.begin() as db:
        return cases.create(db, data, user["username"])


def new_run(case_id, data, user):
    with engine.begin() as db:
        run = runs.enqueue(db, case_id, data.parameter, user["username"])
    try:
        cache.lpush("workbench:wake", str(run["id"]))
        cache.ltrim("workbench:wake", 0, 99)
    except RedisError:
        # The database holds the durable queue. The worker also polls it.
        pass
    return run


def new_transfer(data, user):
    with engine.begin() as db:
        order, _created = transfers.create_order(db, data.model_dump(), user["username"])
        return order


def new_review(run_id, data, user):
    with engine.begin() as db:
        return reviews.sign(db, run_id, data, user["username"])
