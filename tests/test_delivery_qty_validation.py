import pandas as pd

from factory_fee.calculator import calculate


def test_invalid_delivery_qty_creates_exception():
    sap = pd.DataFrame([{
        "raw_id": 1,
        "yyyymm": "202501",
        "factory_code": "F010",
        "movement_type": "101",
        "location_code": "L1",
        "username": "u_bad_qty",
        "material_code": "MAT_BAD_QTY",
        "delivery_qty": 0,
        "_delivery_qty_original": "abc",
    }])
    material = pd.DataFrame([{
        "_snapshot_id": 1,
        "yyyymm": "202501",
        "factory_code": "F010",
        "location_code": "L1",
        "movement_type": "101",
        "username": "u_bad_qty",
        "material_code": "MAT_BAD_QTY",
        "model": "M_BAD_QTY",
        "factory_material_group": "整机",
        "model_category": "CAT_BAD_QTY",
        "process_category": "组装",
        "standard_type": "4G",
        "is_active": "Y",
        "priority": 1,
    }])
    model_coef = pd.DataFrame([{
        "_snapshot_id": 1,
        "yyyymm": "202501",
        "factory_code": "F010",
        "model": "M_BAD_QTY",
        "factory_material_group": "整机",
        "process_category": "组装",
        "shipment_type": "ALL",
        "std_hour_coef": 2,
        "difficulty_coef": 2,
        "is_active": "Y",
        "priority": 1,
    }])
    unit_fee = pd.DataFrame([{
        "_snapshot_id": 1,
        "yyyymm": "202501",
        "factory_code": "F010",
        "model": "M_BAD_QTY",
        "factory_material_group": "整机",
        "process_category": "组装",
        "standard_type": "4G",
        "unit_fee_local": 9,
        "currency_code": "CNY",
        "exchange_rate": 1,
        "is_active": "Y",
        "priority": 1,
    }])
    pnl_rule = pd.DataFrame([{
        "_snapshot_id": 1,
        "yyyymm": "202501",
        "factory_code": "F010",
        "model_category": "CAT_BAD_QTY",
        "process_category": "组装",
        "factory_material_group": "整机",
        "include_pnl_delivery": "是",
        "pnl_delivery_coef": 1,
        "is_active": "Y",
        "priority": 1,
    }])

    result_df, exception_df = calculate("TEST_INVALID_QTY", sap, material, model_coef, unit_fee, pnl_rule)

    assert result_df.loc[0, "calc_status"] == "EXCEPTION"
    assert "DELIVERY_QTY_INVALID" in result_df.loc[0, "exception_reason"]
    assert "DELIVERY_QTY_INVALID" in set(exception_df["exception_code"])
