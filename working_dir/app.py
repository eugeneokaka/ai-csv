import helper
import pandas as pd
import matplotlib.pyplot as plt

df = helper.get_first_csv()

dept_salary = df.groupby('department')['salary'].sum().reset_index()
dept_salary.columns = ['Department', 'Total Salary']

print(dept_salary.to_string(index=False))

fig, ax = plt.subplots(figsize=(8, 5))
colors = ['#4CAF50', '#2196F3', '#FF9800']
bars = ax.bar(dept_salary['Department'], dept_salary['Total Salary'], color=colors)

for bar in bars:
    ax.text(bar.get_x() + bar.get_width()/2., bar.get_height() + 1000,
            f'${bar.get_height():,.0f}', ha='center', va='bottom', fontweight='bold')

ax.set_title('Total Salaries by Department', fontsize=14, fontweight='bold')
ax.set_xlabel('Department')
ax.set_ylabel('Total Salary ($)')
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))
plt.tight_layout()

helper.save_chart(fig, "total_salaries_by_department.png")
helper.save_chart_to_excel(dept_salary, 'Department', 'Total Salary', 'salary_by_department.xlsx')

print("\nChart saved as total_salaries_by_department.png")
print("Excel with chart saved as salary_by_department.xlsx")