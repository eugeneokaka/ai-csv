import helper

df = helper.get_first_csv()

print("Columns in the CSV:")
for i, col in enumerate(df.columns, 1):
    print(f"  {i}. {col}")
print(f"\nTotal columns: {len(df.columns)}")

# Save to Excel for reference
helper.save_excel(df, "employee_data.xlsx")
print(f"\nData saved to: {helper.get_output_path('employee_data.xlsx')}")
