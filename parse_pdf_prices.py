# -*- coding: utf-8 -*-
"""临时脚本：解析项目内 PDF 价格表结构"""
import os
import sys

try:
    import pdfplumber
except ImportError:
    print("请先安装: pip install pdfplumber")
    sys.exit(1)

base = os.path.dirname(os.path.abspath(__file__))
for name in os.listdir(base):
    if not name.lower().endswith('.pdf'):
        continue
    path = os.path.join(base, name)
    print("\n" + "=" * 60)
    print("文件:", name)
    print("=" * 60)
    try:
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages[:3]):
                print("\n--- 第", i + 1, "页 ---")
                tables = page.extract_tables()
                for ti, t in enumerate(tables):
                    print("  表格", ti, "行数:", len(t))
                    for row in t[:12]:
                        print("   ", row)
                if not tables:
                    text = page.extract_text()
                    print("   (无表格) 文本前500字:", (text or "")[:500])
    except Exception as e:
        print("  错误:", e)
