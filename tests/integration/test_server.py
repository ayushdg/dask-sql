from time import sleep

import pandas as pd
import pytest

from dask_sql import Context
from dask_sql.server.app import _init_app, app
from tests.integration.fixtures import DISTRIBUTED_TESTS

# needed for the testclient
pytest.importorskip("requests")


@pytest.fixture(scope="module")
def app_client():
    c = Context()
    c.sql("SELECT 1 + 1").compute()
    _init_app(app, c)

    # late import for the importskip
    from fastapi.testclient import TestClient

    yield TestClient(app)

    # avoid closing client it's session-wide
    if not DISTRIBUTED_TESTS:
        app.client.close()


def get_result_or_error(app_client, response):
    result = response.json()

    assert "nextUri" in result
    assert "error" not in result

    status_url = result["nextUri"]
    next_url = status_url

    counter = 0
    while True:
        response = app_client.get(next_url)
        assert response.status_code == 200

        result = response.json()

        if "nextUri" not in result:
            break

        next_url = result["nextUri"]

        counter += 1
        assert counter <= 100

        sleep(0.1)

    return result


def test_routes(app_client):
    assert app_client.post("/v1/statement", data="SELECT 1 + 1").status_code == 200
    assert app_client.get("/v1/statement", data="SELECT 1 + 1").status_code == 405
    assert app_client.get("/v1/empty").status_code == 200
    assert app_client.get("/v1/status/some-wrong-uuid").status_code == 404
    assert app_client.delete("/v1/cancel/some-wrong-uuid").status_code == 404
    assert app_client.get("/v1/cancel/some-wrong-uuid").status_code == 405


def test_sql_query_cancel(app_client):
    response = app_client.post("/v1/statement", data="SELECT 1 + 1")
    assert response.status_code == 200

    cancel_url = response.json()["partialCancelUri"]

    response = app_client.delete(cancel_url)
    assert response.status_code == 200

    response = app_client.delete(cancel_url)
    assert response.status_code == 404


def test_sql_query(app_client):
    response = app_client.post("/v1/statement", data="SELECT 1 + 1")
    assert response.status_code == 200

    result = get_result_or_error(app_client, response)

    assert "columns" in result
    assert "data" in result
    assert "error" not in result
    assert "nextUri" not in result

    assert result["columns"] == [
        {
            "name": "Int64(1) + Int64(1)",
            "type": "bigint",
            "typeSignature": {"rawType": "bigint", "arguments": []},
        }
    ]
    assert result["data"] == [[2]]


def test_wrong_sql_query(app_client):
    response = app_client.post("/v1/statement", data="SELECT 1 + ")
    assert response.status_code == 200

    result = response.json()

    assert "columns" not in result
    assert "data" not in result
    assert "error" in result
    assert "message" in result["error"]
    # FIXME: ParserErrors currently don't contain information on where the syntax error occurred
    # assert "errorLocation" in result["error"]
    # assert result["error"]["errorLocation"] == {
    #     "lineNumber": 1,
    #     "columnNumber": 10,
    # }


def test_add_and_query(app_client, df, temporary_data_file):
    df.to_csv(temporary_data_file, index=False)

    response = app_client.post(
        "/v1/statement",
        data=f"""
        CREATE TABLE
            new_table
        WITH (
            location = '{temporary_data_file}',
            format = 'csv'
        )
    """,
    )
    result = response.json()
    assert "error" not in result
    assert response.status_code == 200

    response = app_client.post("/v1/statement", data="SELECT * FROM new_table")
    assert response.status_code == 200

    result = get_result_or_error(app_client, response)

    assert "columns" in result
    assert "data" in result
    assert result["columns"] == [
        {
            "name": "a",
            "type": "double",
            "typeSignature": {"rawType": "double", "arguments": []},
        },
        {
            "name": "b",
            "type": "double",
            "typeSignature": {"rawType": "double", "arguments": []},
        },
    ]

    assert len(result["data"]) == 700
    assert "error" not in result


def test_register_and_query(app_client, df):
    df["a"] = df["a"].astype("UInt8")
    app_client.app.c.create_table("new_table", df)

    response = app_client.post("/v1/statement", data="SELECT * FROM new_table")
    assert response.status_code == 200

    result = get_result_or_error(app_client, response)

    assert "columns" in result
    assert "data" in result
    assert result["columns"] == [
        {
            "name": "a",
            "type": "tinyint",
            "typeSignature": {"rawType": "tinyint", "arguments": []},
        },
        {
            "name": "b",
            "type": "double",
            "typeSignature": {"rawType": "double", "arguments": []},
        },
    ]

    assert len(result["data"]) == 700
    assert "error" not in result


def test_inf_table(app_client, user_table_inf):
    app_client.app.c.create_table("new_table", user_table_inf)

    response = app_client.post("/v1/statement", data="SELECT * FROM new_table")
    assert response.status_code == 200

    result = get_result_or_error(app_client, response)

    assert "columns" in result
    assert "data" in result
    assert result["columns"] == [
        {
            "name": "c",
            "type": "double",
            "typeSignature": {"rawType": "double", "arguments": []},
        }
    ]

    assert len(result["data"]) == 3
    assert result["data"][1] == ["+Infinity"]
    assert "error" not in result


def test_nullable_int_table(app_client):
    app_client.app.c.create_table(
        "null_table", pd.DataFrame({"a": [None]}, dtype="Int64")
    )

    response = app_client.post("/v1/statement", data="SELECT * FROM null_table")
    assert response.status_code == 200

    result = get_result_or_error(app_client, response)

    assert "columns" in result
    assert "data" in result
    assert result["columns"] == [
        {
            "name": "a",
            "type": "bigint",
            "typeSignature": {"rawType": "bigint", "arguments": []},
        }
    ]

    assert len(result["data"]) == 1
    assert result["data"][0] == [None]
    assert "error" not in result
