# -*- coding: utf-8 -*-
"""
从项目目录下的 PDF 价格表导入到数据库。
PDF 为纯文本格式（无表格），价格格式如 40r、50r/号、140/图 等。
游戏名从文件名推断：原神、星铁、鸣潮、终末地 等。
运行：在项目目录下执行  python import_prices_from_pdfs.py
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

GAME_FROM_FILENAME = {
    "原神代肝价格表.pdf": "原神",
    "原神代练价格表.pdf": "原神",
    "星穹铁道代肝价格表.pdf": "星铁",
    "星穹铁道代练价格表.pdf": "星铁",
    "鸣潮代肝价格表.pdf": "鸣潮",
    "鸣潮代练价格表.pdf": "鸣潮",
    "明日方舟 - 副本.pdf": "终末地",
    "明日方舟代肝价格表.pdf": "终末地",
    "三角洲代肝价格表.pdf": "三角洲",
    "三角洲代练价格表.pdf": "三角洲",
    "永劫无间代肝价格表.pdf": "永劫无间",
    "永劫无间代练价格表.pdf": "永劫无间",
}


def parse_text_prices(text, game_name):
    rows = []
    price_pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:r|元|R|/号|/图|/天)"
    )
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 2:
            continue
        matches = list(price_pattern.finditer(line))
        if not matches:
            continue
        last = matches[-1]
        try:
            price_val = float(last.group(1))
        except ValueError:
            continue
        if price_val <= 0 or price_val > 99999:
            continue
        task_part = line[: last.start()].strip()
        for prefix in ("V/Q", "QQ", "微信", "全职业", "一.", "二.", "三.", "四.", "五.", "六.", "七.", "1.", "2.", "3."):
            if task_part.startswith(prefix):
                task_part = task_part[len(prefix):].strip()
        if not task_part or len(task_part) < 2:
            task_part = "代练服务"
        task_part = task_part[:50].strip()
        if task_part:
            rows.append((game_name, task_part, round(price_val, 2)))
    return rows


def extract_text_from_pdf(pdf_path):
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("请先安装: pip install pdfplumber")
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


def main():
    from app import app, db
    from models import Price

    with app.app_context():
        all_rows = []
        for filename, game in GAME_FROM_FILENAME.items():
            path = os.path.join(BASE, filename)
            if not os.path.isfile(path):
                print("跳过（不存在）:", filename)
                continue
            print("处理:", filename, "-> 游戏:", game)
            try:
                text = extract_text_from_pdf(path)
                rows = parse_text_prices(text, game)
                all_rows.extend(rows)
                print("  解析到", len(rows), "条")
            except Exception as e:
                print("  错误:", e)

        if not all_rows:
            print("未解析到任何价格，请检查 PDF 格式。")
            return

        added, updated = 0, 0
        for game, task_type, price in all_rows:
            p = Price.query.filter_by(game=game, task_type=task_type).first()
            if p:
                p.price = price
                updated += 1
            else:
                db.session.add(Price(game=game, task_type=task_type, price=price, unit="元/次"))
                added += 1
        db.session.commit()
        print("导入完成：新增", added, "条，更新", updated, "条。")


if __name__ == "__main__":
    main()
