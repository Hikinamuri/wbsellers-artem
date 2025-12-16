from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from ..db import get_db
from ..models import Product, User
from ..schemas import ProductCreate
from ..parser.new_parser import parse_wb_product_api

router = APIRouter(prefix="/products", tags=["products"])

@router.post("/parse")
async def parse_product(data: ProductCreate, db: AsyncSession = Depends(get_db)):
    product_data = await parse_wb_product_api(data.url)

    user = await db.get(User, data.user_id)
    if not user:
        return {"success": False, "error": "User not found"}

    new_product = Product(
        user_id=user.id,
        url=data.url,
        name=product_data.get("name"),
        description=product_data.get("description"),
        image_url=product_data.get("images", [None])[0],
        info=product_data,
        status="В обработке"
    )
    db.add(new_product)
    await db.commit()
    await db.refresh(new_product)
    return {"success": True, "product": new_product}
