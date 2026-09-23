import helper
import pandas as pd
import matplotlib.pyplot as plt

df = helper.get_first_csv()

fig, ax = plt.subplots(figsize=(8, 5))
df.groupby("employment_type")["salary"].mean().plot(kind="bar", ax=ax, color=["#4472C4", "#ED7D31"])
ax.set_title("Employment Type vs Salary")
ax.set_xlabel("Employment Type")
ax.set_ylabel("Salary ($)")
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
plt.tight_layout()

helper.save_chart(fig, "employment_type_vs_salary.png")
print("Chart saved!")
print(df.groupby("employment_type")["salary"].mean().to_string())