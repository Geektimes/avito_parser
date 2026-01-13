# models.py
from tortoise import fields, models

class Advertisement(models.Model):
    # ID объявления (BigInt, так как ID Avito длинные, и это первичный ключ)
    id = fields.BigIntField(pk=True)
    
    # Основные поля
    title = fields.CharField(max_length=255, null=True)
    price = fields.IntField(null=True)
    
    # ID продавца (извлекаем из ссылки на профиль)
    seller_id = fields.CharField(max_length=100, null=True)
    
    # Дата и время (строкой, т.к. Avito пишет "1 час назад", "Вчера" и т.д.)
    date = fields.CharField(max_length=100, null=True)
    
    # Краткое описание
    legend = fields.TextField(null=True)
    
    # Город/Локация
    city = fields.CharField(max_length=100, null=True)
    
    # Ссылка на фото
    photo = fields.TextField(null=True)
    
    # Булево поле (избранное)
    is_favorite = fields.BooleanField(default=False)

    # Техническое поле даты добавления в нашу базу
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "advertisements"
        
    def __str__(self):
        return f"{self.id} - {self.title}"
