from sqlalchemy import (
    Column, Integer, String, Float, Text, DateTime, ForeignKey,
    Enum, BigInteger
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum
from .db import Base


class ProductStatus(str, enum.Enum):
    processing = "В обработке"
    pending = "Ожидает выкладки"
    posted = "Выложен"
    canceled = "Отменен"
    
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    tg_id = Column(String, unique=True)
    name = Column(String)
    phone = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    products = relationship("Product", back_populates="user")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    user_id = Column(String, ForeignKey("users.tg_id"))
    url = Column(String)
    name = Column(String)
    description = Column(Text)
    image_url = Column(String)

    # 🔹 Совместимые с таблицей типы
    wb_id = Column(BigInteger, nullable=True)
    brand = Column(String, nullable=True)
    seller = Column(String, nullable=True)
    rating = Column(Float, nullable=True)
    feedbacks = Column(Integer, nullable=True)
    basic_price = Column(Float, nullable=True)
    discount = Column(Integer, nullable=True)
    stocks = Column(Integer, nullable=True)
    stocks_by_size = Column(JSONB, nullable=True)
    images = Column(JSONB, nullable=True)
    category = Column(String(50), nullable=True)

    info = Column(JSONB, nullable=True)
    status = Column(Enum(ProductStatus), default=ProductStatus.processing)
    scheduled_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    price = Column(Float, nullable=True)

    user = relationship("User", back_populates="products")
