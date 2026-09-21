import helper
import matplotlib.pyplot as plt

df = helper.get_first_csv()

fig, ax = plt.subplots(figsize=(8, 5))
ax.scatter(df["years_experience"], df["salary"], color="steelblue", s=100, edgecolors="black")

ax.set_xlabel("Years of Experience")
ax.set_ylabel("Salary")
ax.set_title("Years of Experience vs Salary")
ax.grid(True, linestyle="--", alpha=0.7)

for i, row in df.iterrows():
    ax.annotate(row["name"], (row["years_experience"], row["salary"]),
                textcoords="offset points", xytext=(5, 5), fontsize=9)

plt.tight_layout()

print("Years of Experience vs Salary:")
for _, row in df.iterrows():
    print(f"  {row['name']}: {row['years_experience']} years -> {row['salary']:,.0f}")

helper.save_chart(fig, "experience_vs_salary.png")