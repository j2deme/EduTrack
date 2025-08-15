import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-key-123')
    MONGO_URI = os.getenv('MONGO_URI') or \
        f"mongodb://{os.getenv('MONGO_USER')}:{os.getenv('MONGO_PASSWORD')}@localhost:27017/{os.getenv('MONGO_DB')}?authSource=admin"
    PASSWORD_HASH_METHOD = 'pbkdf2:sha256'
    PASSWORD_SALT_LENGTH = 16
