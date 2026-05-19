import pandas as pd
from factory_fee.matcher import find_best_match, MATERIAL_SPECS


def test_material_match_priority_3():
    row = {
        "yyyymm": "202502",
        "factory_code": "F001",
        "location_code": "L1",
        "movement_type": "101",
        "username": "u1",
        "material_code": "MAT001",
    }
    rules = pd.DataFrame([
        {"yyyymm": "202502", "factory_code": "F001", "location_code": "ALL", "movement_type": "ALL", "username": "ALL", "material_code": "MAT001", "priority": 3, "is_active": "Y", "model": "M1"},
    ])
    out = find_best_match(row, rules, MATERIAL_SPECS)
    assert out.status == "MATCHED"
    assert out.priority == 3
    assert out.rule["model"] == "M1"
