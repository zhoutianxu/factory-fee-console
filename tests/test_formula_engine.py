from factory_fee.formula_engine import evaluate_calculated_columns, evaluate_formula


def test_formula_supports_default_fee_columns_in_order():
    record = {
        "delivery_qty": 10,
        "pnl_delivery_coef": 0.8,
        "include_pnl_delivery": "是",
        "unit_fee_local": 3,
        "currency_code": "USD",
        "exchange_rate": 7,
    }
    columns = [
        {"field": "pnl_delivery_qty", "formula": 'if_in(include_pnl_delivery, "是,Y", num(delivery_qty) * num(pnl_delivery_coef), 0)', "active": "Y"},
        {"field": "unit_fee_cny", "formula": 'if_eq(currency_code, "CNY", num(unit_fee_local), num(unit_fee_local) * num(exchange_rate))', "active": "Y"},
        {"field": "fee_amount_cny", "formula": "num(pnl_delivery_qty) * num(unit_fee_cny)", "active": "Y"},
    ]

    out = evaluate_calculated_columns(record, columns)

    assert out["pnl_delivery_qty"] == 8
    assert out["unit_fee_cny"] == 21
    assert out["fee_amount_cny"] == 168


def test_formula_supports_chinese_bracket_references():
    result = evaluate_formula("[交货数量] * 2", {"delivery_qty": 5}, label_map={"交货数量": "delivery_qty"})

    assert result == 10
