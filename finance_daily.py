import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.utils import executor
import datetime

TOKEN = "8685677901:AAELfGxR1usKTsqUiH4rDFM2CfyF8rJ3qq4"

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

# ТВОИ ЛИМИТЫ
MONTH_LIMIT = 80000

# ВРЕМЕННО: будем хранить в памяти
expenses = []

def calculate_total():
    return sum(e["amount"] for e in expenses)

@dp.message_handler()
async def handle_message(message: Message):
    try:
        text = message.text.lower()
        parts = text.split()

        if len(parts) < 2:
            await message.answer("Формат: категория сумма\nпример: такси 350")
            return

        category = parts[0]
        amount = int(parts[1])

        expenses.append({
            "date": datetime.datetime.now(),
            "user": message.from_user.first_name,
            "category": category,
            "amount": amount
        })

        total = calculate_total()
        remaining = MONTH_LIMIT - total
        days_left = 30 - datetime.datetime.now().day
        daily = remaining // max(days_left, 1)

        await message.answer(
            f"Ок 👌\n"
            f"Потрачено: {total} ₽\n"
            f"Осталось: {remaining} ₽\n"
            f"На день: {daily} ₽"
        )

    except:
        await message.answer("Ошибка. Пиши: такси 350")

if __name__ == "__main__":
    executor.start_polling(dp)