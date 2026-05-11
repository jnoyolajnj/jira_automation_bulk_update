"""Generates a sample input Excel file with the structure the checker expects."""
from openpyxl import Workbook

wb = Workbook()
HEADER = ["User Story", "Stream"]

ws_fin = wb.active
ws_fin.title = "Finance"
ws_fin.append(HEADER)
ws_fin.append(["AASQ-72454", "Finance"])

for sheet_name in ("Delivery", "Distribution", "Loaner", "EDI", "Planning"):
    ws = wb.create_sheet(sheet_name)
    ws.append(HEADER)

wb.save("input_user_stories.xlsx")
print("Created input_user_stories.xlsx")
