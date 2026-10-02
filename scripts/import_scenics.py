"""导入 CSV 景区数据到 numeri_core_sys_scenics 表。

用法：uv run python3 scripts/import_scenics.py
"""

import csv
import asyncio
import asyncpg

CSV_PATH = "pft_ai_dim_prd_scenic_base_yr_f.csv"
BATCH_SIZE = 500

DB_CONFIG = dict(
    host="localhost", port=5432, user="zhilv",
    password="zhilv_dev_2026", database="zhilv",
)


async def main():
    conn = await asyncpg.connect(**DB_CONFIG)

    # 1. 清空旧数据
    await conn.execute("DELETE FROM numeri_core_sys_scenics")
    print("已清空旧数据")

    # 2. 读取 CSV（无表头，按列顺序对应源表字段）
    COLUMNS = [
        "scenic_id", "scenic_name", "scenic_alias", "scenic_name_en", "scenic_appid",
        "country_name", "province_name", "city_name", "district_name", "scenic_address",
        "scenic_lng", "scenic_lat", "scenic_area_mu", "scenic_mountain_river",
        "scenic_grade_level", "scenic_grade_announce_no", "scenic_grade_first_time",
        "scenic_grade_review_time", "scenic_heritage_cert", "scenic_national_title",
        "scenic_operator_name", "scenic_superior_dept", "scenic_tel_consult",
        "scenic_tel_complaint", "scenic_tel_emergency", "scenic_website_url",
        "scenic_official_appid", "is_free_flag", "ticket_adult_weekday_amt",
        "ticket_adult_peak_amt", "ticket_adult_low_amt", "ticket_preferential_amt",
        "ticket_transit_amt", "ticket_combo_amt", "ticket_advance_days_qty",
        "has_online_ticket_flag", "open_status_type", "open_time_peak",
        "close_time_peak", "open_time_low", "close_time_low", "last_entry_time",
        "special_open_rule_desc", "suggest_duration_desc", "best_season_desc",
        "scenic_main_type", "scenic_sub_type", "scenic_feature_tags", "scenic_desc",
        "scenic_core_spots", "daily_capacity_max_qty", "instant_capacity_max_qty",
        "last_year_visitor_qty", "has_parking_flag", "parking_space_qty",
        "has_guide_flag", "guide_language_type", "create_time", "update_time",
        "travel_date",
    ]

    rows = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for line in reader:
            if len(line) < 3:
                continue
            row = dict(zip(COLUMNS, line))
            rows.append((
                row["scenic_id"],
                row["scenic_name"] or "",
                row.get("scenic_grade_level") or None,
            ))

    print(f"CSV 共 {len(rows)} 条记录")

    # 3. 批量写入
    sql = """
        INSERT INTO numeri_core_sys_scenics (scenic_id, scenic_name, scenic_grade_level, is_enabled, plan_config, synced_at)
        VALUES ($1, $2, $3, true, '{}'::json, NOW())
    """
    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        await conn.executemany(sql, batch, timeout=30)
        inserted += len(batch)
        print(f"  已写入 {inserted}/{len(rows)}")

    # 4. 验证
    count = await conn.fetchval("SELECT count(*) FROM numeri_core_sys_scenics")
    print(f"\n导入完成，numeri_core_sys_scenics 共 {count} 条记录")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
