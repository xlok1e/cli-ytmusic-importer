import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.prompt import Prompt
from ytmusicapi import YTMusic

console = Console()

MAX_RETRIES = 5
RETRY_BASE_DELAY = 5
CHECKPOINT_FILE = "checkpoint.json"
BROWSER_AUTH_FILE = "browser.json"
SEARCH_BATCH = 250 
ADD_BATCH = 1000    


def save_checkpoint(tracks_file: str, playlist_id: str, next_index: int, imported_count: int) -> None:
    with open(CHECKPOINT_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "tracks_file": tracks_file,
            "playlist_id": playlist_id,
            "next_index": next_index,
            "imported_count": imported_count,
        }, f)


def load_checkpoint() -> Optional[dict]:
    if not os.path.exists(CHECKPOINT_FILE):
        return None
    try:
        with open(CHECKPOINT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def clear_checkpoint() -> None:
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


def _search_one(ytm: YTMusic, query: str) -> Optional[str]:
    """Ищет один трек с retry. Бросает исключение при 401."""
    for attempt in range(MAX_RETRIES):
        try:
            found = ytm.search(query, filter="songs", limit=1)
            if not found:
                found = ytm.search(query, filter="videos", limit=1)
            return found[0]['videoId'] if found else None
        except Exception as e:
            if "401" in str(e):
                raise
            if attempt == MAX_RETRIES - 1:
                return None
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    return None


def search_batch(ytm: YTMusic, queries: list, progress_console: Console) -> list:
    """Запускает все запросы батча одновременно, ждёт все результаты."""
    results: list = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        future_to_idx = {pool.submit(_search_one, ytm, q): i for i, q in enumerate(queries)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                if "401" in str(e):
                    raise
                progress_console.print(f"[red]Пропущен:[/red] {queries[idx]} [dim]({str(e)[:60]})[/dim]")
    return results


def add_to_playlist(ytm: YTMusic, playlist_id: str, video_ids: list, progress_console: Console) -> None:
    for attempt in range(MAX_RETRIES):
        try:
            ytm.add_playlist_items(playlist_id, video_ids, duplicates=True)
            return
        except Exception as e:
            if "401" in str(e):
                raise
            wait = RETRY_BASE_DELAY * (2 ** attempt)
            progress_console.print(f"[yellow]⚠ Ошибка добавления — повтор через {wait}с...[/yellow]")
            time.sleep(wait)
    raise RuntimeError(f"Не удалось добавить батч после {MAX_RETRIES} попыток")


def show_welcome():
    console.print(Panel.fit(
        "Авторизация: [bold white]browser.json[/bold white]\n"
        "Команды: [bold cyan]/start[/bold cyan] - запуск, [bold cyan]/help[/bold cyan] - справка, [bold cyan]/exit[/bold cyan] - выход",
        border_style="red"
    ))


def show_help():
    console.print(Panel(
        "\n"
        "  [bold cyan]ШАГ 1 — АВТОРИЗАЦИЯ[/bold cyan]\n\n"
        "  Откройте [bold white]https://music.youtube.com[/bold white] в браузере и убедитесь,\n"
        "  что вы вошли в аккаунт.\n\n"
        "  Откройте DevTools ([bold]F12[/bold] / [bold]Command+Option+I[/bold]) → вкладка [bold]Network[/bold].\n"
        "  Поставьте лайк любому треку — в Network появится POST-запрос.\n\n"
        "  [bold yellow]Firefox:[/bold yellow] ПКМ по запросу → Copy → [bold]Copy Request Headers[/bold]\n"
        "  [bold yellow]Chrome/Edge:[/bold yellow] Нажмите на запрос → вкладка Headers →\n"
        "              скопируйте раздел [bold]Request Headers[/bold]\n\n"
        "  Затем в терминале выполните:\n\n"
        "    [bold green]ytmusicapi browser[/bold green]\n\n"
        "  Скрипт попросит вставить скопированные заголовки.\n\n"
        "  [bold white]Windows:[/bold white] вставить — [bold]Ctrl+V[/bold] или ПКМ в терминал\n"
        "  [bold white]macOS:[/bold white]   из-за ограничения терминала (1024 символа) используйте:\n\n"
        "    [bold green]pbpaste | python main.py[/bold green]   — это передаёт буфер обмена в скрипт.\n\n"
        "  После вставки нажмите [bold]Enter[/bold], затем [bold]Ctrl+D[/bold] (конец ввода).\n"
        "  В папке создастся файл [bold white]browser.json[/bold white] с учётными данными.\n"
        "  Токен действителен [bold]~2 года[/bold] (пока вы не выйдете из браузера).\n\n"
        "  ─────────────────────────────────────────────────\n\n"
        "  [bold cyan]ШАГ 2 — ЗАПУСК ИМПОРТА[/bold cyan]\n\n"
        "  Запустите скрипт и введите команду [bold yellow]/start[/bold yellow].\n"
        "  Скрипт спросит:\n\n"
        "    [dim]1.[/dim] [bold white]Имя файла с треками[/bold white] (по умолчанию [white]tracks.txt[/white])\n"
        "         Формат файла — одна строка = один трек:\n"
        "         [dim]Исполнитель - Название трека[/dim]\n\n"
        "    [dim]2.[/dim] [bold white]Название нового плейлиста[/bold white]\n\n"
        "  После этого дождитесь завершения — скрипт сам найдёт\n"
        "  каждый трек и добавит его в плейлист.\n\n"
        "  ─────────────────────────────────────────────────\n\n"
        "  [bold red]⚠  ВАЖНО:[/bold red]\n"
        "  • [bold white]Ctrl+C[/bold white] — безопасно прервать; прогресс сохранится,\n"
        "    при следующем [bold]/start[/bold] предложит продолжить.\n"
        "  • Не выходите из Google/YouTube в браузере,\n"
        "    которым создавали browser.json.\n"
        "  • Скрипт использует [bold yellow]неофициальный API ytmusicapi[/bold yellow].\n"
        "    Данные пользователей [bold]никуда не передаются и не сохраняются[/bold] скриптом.\n",
        title="📖  Справка", border_style="blue", expand=False
    ))


def start_import_flow():
    if not os.path.exists(BROWSER_AUTH_FILE):
        console.print(
            f"\n[bold red]❌ ОШИБКА АВТОРИЗАЦИИ[/bold red]\n"
            f"Файл [white]{BROWSER_AUTH_FILE}[/white] не найден.\n"
            f"Решение: выполните [bold yellow]ytmusicapi browser[/bold yellow] в терминале."
        )
        return

    try:
        ytm = YTMusic(BROWSER_AUTH_FILE)
        console.print("\n[green]✅ Авторизация подтверждена (browser.json найден).[/green]")

        checkpoint = load_checkpoint()
        resuming = False
        file_name: str
        playlist_id: str
        start_index: int
        imported_count: int

        if checkpoint:
            console.print()
            console.print(Panel(
                f"[yellow]Найден незавершённый импорт![/yellow]\n\n"
                f"Файл треков: [white]{checkpoint['tracks_file']}[/white]\n"
                f"Продолжить с трека: [white]#{checkpoint['next_index'] + 1}[/white]\n"
                f"Уже добавлено: [white]{checkpoint['imported_count']}[/white]",
                title="Возобновить?", border_style="yellow", expand=False
            ))
            answer = Prompt.ask("[yellow]Продолжить с места остановки?[/yellow]", choices=["да", "нет"], default="да")
            if answer == "да":
                resuming = True
                file_name = checkpoint['tracks_file']
                playlist_id = checkpoint['playlist_id']
                start_index = checkpoint['next_index']
                imported_count = checkpoint['imported_count']
            else:
                clear_checkpoint()

        if not resuming:
            console.print("\n[bold red]>[/bold red] [bold]ВЫБОР ФАЙЛА[/bold]")
            file_name = Prompt.ask("[yellow]Введите имя файла с треками[/yellow]", default="tracks.txt")

            if not os.path.exists(file_name):
                console.print(f"[bold red]❌ Ошибка: Файл '{file_name}' не найден![/bold red]")
                return

            pl_name = Prompt.ask("\n[bold red]>[/bold red] [bold]НАЗВАНИЕ ПЛЕЙЛИСТА[/bold]", default="My Imported Playlist")

            with console.status("[bold red]Создаём пустой плейлист..."):
                playlist_id = ytm.create_playlist(pl_name, "Created via YTMUSIC IMPORTER (Developer: t.me/egorlok1e)")

            start_index = 0
            imported_count = 0

        with open(file_name, 'r', encoding='utf-8') as f:
            tracks = [line.strip() for line in f if line.strip()]

        console.print(f"[blue]ℹ Всего треков: {len(tracks)}[/blue]")
        if resuming:
            console.print(f"[blue]ℹ Продолжаем с трека #{start_index + 1}[/blue]")

        console.print(f"\n[bold yellow]🚀 Начинаем импорт треков в YouTube Music...[/bold yellow]\n")

        remaining = tracks[start_index:]
        pending_ids: list = []
        token_expired = False

        with Progress(
            SpinnerColumn(spinner_name="simpleDotsScrolling", style="red"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=40, style="black", complete_style="red"),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[red]Поиск...", total=len(tracks), completed=start_index)

            offset = 0
            while offset < len(remaining):
                batch_queries = remaining[offset:offset + SEARCH_BATCH]
                actual_start = start_index + offset

                try:
                    found_ids = search_batch(ytm, batch_queries, progress.console)
                except Exception as e:
                    if "401" in str(e):
                        if pending_ids:
                            try:
                                add_to_playlist(ytm, playlist_id, pending_ids, progress.console)
                                imported_count += len(pending_ids)
                            except Exception:
                                pass
                            pending_ids = []
                        save_checkpoint(file_name, playlist_id, actual_start, imported_count)
                        token_expired = True
                        progress.console.print(f"\n[bold red]❌ Токен истёк! Прогресс сохранён (трек #{actual_start + 1}).[/bold red]")
                        break
                    raise

                for query, vid in zip(batch_queries, found_ids):
                    if vid:
                        pending_ids.append(vid)
                        progress.console.print(f"[dim]Найден:[/dim] [white]{query}[/white]")
                    else:
                        progress.console.print(f"[yellow]Не найден:[/yellow] {query}")

                offset += len(batch_queries)
                progress.update(task, advance=len(batch_queries))

                # Сбрасываем в плейлист каждые ADD_BATCH найденных треков
                if len(pending_ids) >= ADD_BATCH:
                    try:
                        add_to_playlist(ytm, playlist_id, pending_ids, progress.console)
                        imported_count += len(pending_ids)
                        progress.console.print(f"[green]✓ Добавлено в плейлист: {imported_count} треков[/green]")
                        pending_ids = []
                    except Exception as e:
                        if "401" in str(e):
                            save_checkpoint(file_name, playlist_id, actual_start + len(batch_queries), imported_count)
                            token_expired = True
                            break
                        raise

                save_checkpoint(file_name, playlist_id, actual_start + len(batch_queries), imported_count)

            # Остаток < ADD_BATCH
            if not token_expired and pending_ids:
                try:
                    add_to_playlist(ytm, playlist_id, pending_ids, progress.console)
                    imported_count += len(pending_ids)
                    pending_ids = []
                except Exception as e:
                    if "401" in str(e):
                        save_checkpoint(file_name, playlist_id, len(tracks), imported_count)
                        token_expired = True
                    else:
                        raise

        url = f"https://music.youtube.com/playlist?list={playlist_id}"

        if token_expired:
            console.print()
            console.print(Panel(
                f"[bold red]ИМПОРТ ПРЕРВАН — ТОКЕН ИСТЁК[/bold red]\n\n"
                f"Добавлено: [bold]{imported_count}[/bold] из {len(tracks)}\n"
                f"Плейлист: [bold cyan][link={url}]{url}[/link][/bold cyan]\n\n"
                f"[yellow]Что делать:[/yellow]\n"
                f"1. Выполните [bold green]ytmusicapi browser[/bold green] в терминале\n"
                f"2. Запустите [bold cyan]/start[/bold cyan] — прогресс восстановится",
                title="Прерван", border_style="red", expand=False
            ))
            return

        clear_checkpoint()
        console.print()
        console.print(Panel(
            f"[bold green]ИМПОРТ ЗАВЕРШЕН![/bold green]\n\n"
            f"Успешно добавлено: [bold]{imported_count}[/bold] из {len(tracks)}\n"
            f"Ссылка на плейлист: [bold cyan][link={url}]{url}[/link][/bold cyan]",
            title="Готово", border_style="green", expand=False
        ))

    except KeyboardInterrupt:
        console.print("\n\n[bold red]ОТМЕНА[/bold red] — импорт прерван вручную.")
    except Exception as e:
        console.print(f"\n[bold red]Критическая ошибка: {e}[/bold red]")


def main():
    show_welcome()
    while True:
        try:
            cmd = Prompt.ask("\n[bold red]YTM[/bold red] [bold white]❯[/bold white]").lower().strip()
            if cmd == "/start":
                start_import_flow()
            elif cmd == "/help":
                show_help()
            elif cmd == "/exit":
                console.print("[italic dim]Выходим...[/italic dim]")
                sys.exit()
            elif not cmd:
                continue
            else:
                console.print("[red]⚠ Неизвестная команда. Введите [bold]/help[/bold][/red]")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Выход...[/dim]")
            break


if __name__ == "__main__":
    main()
