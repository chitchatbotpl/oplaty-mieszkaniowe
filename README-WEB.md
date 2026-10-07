# Opłaty mieszkaniowe — wersja WWW v2

Ta wersja jest przygotowana do wdrożenia jako prawdziwa aplikacja internetowa:
- Flask + Gunicorn
- PostgreSQL na serwerze
- SQLite pozostaje jako lokalny fallback
- aplikacja nasłuchuje przez Gunicorn na serwerze
- Render może utworzyć usługę WWW i bazę na podstawie `render.yaml`

## Najprostsze wdrożenie

1. Załóż konto na Render.
2. Umieść ten katalog w prywatnym repozytorium GitHub.
3. W Render wybierz New -> Blueprint i wskaż repozytorium.
4. Render odczyta `render.yaml`, utworzy aplikację oraz PostgreSQL.
5. Po wdrożeniu dostaniesz publiczny adres HTTPS.

## Lokalnie

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py

Bez DATABASE_URL używany jest lokalny plik `oplaty.db`.
