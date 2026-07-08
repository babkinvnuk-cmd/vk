# VK Video Proxy

Прокси для доступу до VK Video API з обходом обмежень.

## Деплой на Render

1. Форкни цей репозиторій або створи новий
2. Йди на [render.com](https://render.com)
3. Створи новий **Web Service**
4. Підключи GitHub репозиторій
5. Налаштування:
   - **Name**: vkvideo-proxy (або будь-яка назва)
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free

## Використання

API буде доступний за адресою: `https://your-app-name.onrender.com`

### Ендпоінти:

- `GET /` - статус сервера
- `GET /vkvideo/search?q=query&offset=0&count=50` - пошук відео
- `GET /vkmovie/search?q=query&year=2024` - пошук фільмів (тільки довгі)
- `GET /vkmovie/stream?url=VIDEO_URL` - проксування відео

## Локальний запуск

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

Сервер запуститься на http://localhost:8000
