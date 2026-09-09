from connection import Base, engine
from queries import User, NatalChart, ChartData, EmailVerificationCode, ChartInterpretationData



print("🛠 Создание таблиц...")
Base.metadata.create_all(bind=engine)
print("✅ Таблицы созданы!")
