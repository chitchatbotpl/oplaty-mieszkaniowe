# Opłaty mieszkaniowe v3

Aplikacja Flask do zarządzania mieszkaniami, własnymi sekcjami/opłatami, odczytami liczników, kwotami i płatnościami.

Render: Build `pip install -r requirements.txt`, Start `gunicorn app:app`.


## Bezpieczna aktualizacja
Ta wersja nie zmienia schematu bazy danych i nie usuwa ani nie nadpisuje istniejących mieszkań, sekcji ani odczytów. Na Renderze dane PostgreSQL są niezależne od plików aplikacji. Aktualizacja dotyczy wyboru okresu (rok + miesiąc) i wyglądu pól wyboru.
