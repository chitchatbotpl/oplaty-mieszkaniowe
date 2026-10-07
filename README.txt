OPLATY MIESZKANIOWE - wersja 1.0

Wymagania: Python 3.10+.

Windows:
1. Otwórz PowerShell w tym folderze.
2. python -m venv .venv
3. .venv\Scripts\activate
4. pip install -r requirements.txt
5. python app.py
6. Otwórz http://127.0.0.1:5000

Baza SQLite (oplaty.db) tworzy się automatycznie przy pierwszym uruchomieniu.
Aplikacja dodaje 3 przykładowe mieszkania tylko przy pustej bazie.
Eksport CSV działa z menu po lewej.

To jest pierwsza wersja funkcjonalna. Następny etap może obejmować użytkowników/logowanie,
PDF, Excel, automatyczne naliczenia, liczniki, przypomnienia i kopie zapasowe.
