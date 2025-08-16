import gradio as gr
import pandas as pd
import os
import re
import time
from pathlib import Path
import shutil
import zipfile
import uuid
import tempfile
import yt_dlp # yt-dlpをライブラリとしてインポート

# --- ヘルパー関数 ---

def sanitize_filename(name):
    """ファイル名として使えない文字を安全な文字に置換します。"""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

# --- Gradio アプリケーションのコアロジック ---

def load_csv_and_prepare(uploaded_file):
    """
    CSVファイルを読み込み、処理の準備をします。
    """
    if uploaded_file is None:
        return None, 0, "エラー: CSVファイルをアップロードしてください。", gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    file_path = uploaded_file.name
    data = []
    try:
        with open(file_path, 'r', encoding='cp932') as f:
            next(f)
            for line in f:
                parts = line.strip().split(',', 1)
                if len(parts) == 2:
                    data.append({'曲名': parts[0].strip(), 'バンド名': parts[1].strip()})
    except Exception as e:
        return None, 0, f"ファイルの読み込みエラー: {e}", gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    df = pd.DataFrame(data)
    total_songs = len(df)
    log_message = f"CSV読み込み完了。合計 {total_songs} 曲。\nダウンロード準備OKです。"
    
    return df, 0, log_message, gr.update(visible=True, interactive=True), gr.update(visible=False), gr.update(visible=True)


def download_batch(song_df, current_index, progress=gr.Progress(track_tqdm=True)):
    """
    yt-dlpライブラリとCookieを使って曲をダウンロードし、ZIP化します。
    """
    if song_df is None or song_df.empty:
        yield "エラー: 曲リストが読み込まれていません。", None, gr.update(interactive=False)
        return

    # --- Cookie処理 ---
    cookie_secret_name = 'YOUTUBE_COOKIES'
    cookie_content = os.getenv(cookie_secret_name)
    if not cookie_content:
        print("Cookieが設定されていません。")
    temp_cookie_file = None
    cookie_file_path = None

    if cookie_content:
        try:
            temp_cookie_file = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".txt", encoding='utf-8')
            temp_cookie_file.write(cookie_content)
            cookie_file_path = temp_cookie_file.name
            temp_cookie_file.close()
        except Exception:
            cookie_file_path = None
    
    # --- ダウンロード処理 ---
    session_id = str(uuid.uuid4())
    batch_dir = Path(f"/tmp/music_batch_{session_id}")
    batch_dir.mkdir(exist_ok=True)

    start_index = current_index
    end_index = min(start_index + 20, len(song_df))
    batch_df = song_df.iloc[start_index:end_index]
    
    log = f"バッチ {start_index//20 + 1} ({start_index + 1}〜{end_index}曲目) を開始します。\n"
    log += f"Cookieの使用: {'あり' if cookie_file_path else 'なし (Secret `YOUTUBE_COOKIES` が未設定の可能性)'}\n"
    yield log, None, gr.update(interactive=False)

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'outtmpl': str(batch_dir / '%(title)s.%(ext)s'),
        'noplaylist': True,
        'cookiefile': cookie_file_path,
        'cachedir': f'/tmp/yt-dlp-cache-{session_id}',
        'quiet': True,
        'no_warnings': True,
        'force_ipv4': True, # ネットワーク安定化のためIPv4を強制
        'socket_timeout': 15, # 15秒でタイムアウト
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for _, row in progress.tqdm(batch_df.iterrows(), total=len(batch_df), desc=f"バッチ {start_index//20 + 1} を処理中"):
                song_title = row['曲名']
                artist_name = row['バンド名']
                search_query = f"ytsearch1:{artist_name} {song_title}"

                log += f"  > {artist_name} - {song_title} を検索・ダウンロード中..."
                yield log, None, gr.update(interactive=False)
                
                # --- リトライロジック ---
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        ydl.download([search_query])
                        log += " ✔ 成功\n"
                        break # 成功したらループを抜ける
                    except yt_dlp.utils.DownloadError as e:
                        error_message = str(e).lower()
                        is_network_error = "resolve" in error_message or "download api page" in error_message
                        
                        if is_network_error and attempt < max_retries - 1:
                            log += f" ⚠️ ネットワークエラー。再試行 ({attempt + 2}/{max_retries})...\n"
                            time.sleep(2) # 2秒待機
                            continue # 次の試行へ
                        
                        if "sign in" in error_message or "confirm your age" in error_message:
                            log += " ❌ 失敗 (年齢確認/ログインが必要。Cookieが無効な可能性があります)\n"
                        else:
                            log += f" ❌ 失敗 ({type(e).__name__})\n"
                        break # リトライ不可能なエラーか、最大回数に達した
                    except Exception as e:
                        log += f" ❌ 予期せぬエラー ({type(e).__name__})\n"
                        break # 予期せぬエラーはリトライしない
                
                yield log, None, gr.update(interactive=False)

    finally:
        # 一時Cookieファイルを確実に削除
        if cookie_file_path and os.path.exists(cookie_file_path):
            os.remove(cookie_file_path)

    # ZIPファイルを作成
    zip_path = f"/tmp/music_batch_{start_index//20 + 1}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        mp3_files = list(batch_dir.glob('*.mp3'))
        if mp3_files:
            for file in mp3_files:
                sanitized_name = sanitize_filename(file.name)
                zipf.write(file, sanitized_name)
        else:
            with open(batch_dir / "no_files_downloaded.txt", "w") as f:
                f.write("このバッチではダウンロードに成功したファイルがありませんでした。")
            zipf.write(batch_dir / "no_files_downloaded.txt", "no_files_downloaded.txt")

    shutil.rmtree(batch_dir)

    new_index = end_index
    is_last_batch = new_index >= len(song_df)
    
    log += f"\nバッチ {start_index//20 + 1} の処理完了。ZIPファイルをダウンロードできます。"
    if not is_last_batch:
        log += "\n準備ができたら次のバッチを開始してください。"
    else:
        log += "\nすべての曲の処理が完了しました。"

    yield log, gr.update(value=zip_path, visible=True), gr.update(interactive=not is_last_batch)
    

def delete_all_data():
    """/tmp に作成された関連ファイルをすべて削除します。"""
    count = 0
    for path in Path("/tmp").glob("music_batch_*"):
        if path.is_file(): path.unlink()
        elif path.is_dir(): shutil.rmtree(path)
        count += 1
    return f"クリーンアップ完了。{count}個の関連ファイル/フォルダを削除しました。"


# --- Gradio UIの構築 ---
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    song_data = gr.State(None)
    current_index = gr.State(0)

    gr.Markdown("# 🎵 YouTube Music Downloader (Cookie対応版)")
    gr.Markdown(
        """
        **重要:** このアプリを動作させるには、Hugging Face Spaceの **Settings > Secrets** に `YOUTUBE_COOKIES` という名前で、ブラウザからエクスポートしたNetscape形式のCookie文字列を設定する必要があります。
        1. CSVファイルをアップロードし、「CSVを読み込む」ボタンを押してください。
        2. 「曲のダウンロードを開始」ボタンで、20曲ずつのダウンロードとZIP化が始まります。
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            file_input = gr.File(label="CSVファイルをアップロード (cp932/Shift-JIS)", file_types=[".csv"])
            prepare_button = gr.Button("1. CSVを読み込む", variant="secondary")
            download_button = gr.Button("2. 曲のダウンロードを開始 / 次の20曲", variant="primary", visible=False)
            zip_download_component = gr.File(label="ZIPファイルをダウンロード", visible=False)
            cleanup_button = gr.Button("一時ファイルを全て削除", variant="stop", visible=False)

        with gr.Column(scale=2):
            progress_output = gr.Textbox(label="進捗状況", lines=15, interactive=False, autoscroll=True)

    prepare_button.click(
        fn=load_csv_and_prepare,
        inputs=file_input,
        outputs=[song_data, current_index, progress_output, download_button, zip_download_component, cleanup_button]
    )

    download_button.click(
        fn=download_batch,
        inputs=[song_data, current_index],
        outputs=[progress_output, zip_download_component, download_button]
    ).then(
        fn=lambda idx: idx + 20,
        inputs=current_index,
        outputs=current_index
    )

    cleanup_button.click(fn=delete_all_data, inputs=[], outputs=[progress_output])

import os # Make sure this is at the top of your file

if __name__ == "__main__":
    # Render provides the port number via the PORT environment variable
    # We use 7860 as a default for running locally
    port = int(os.getenv('PORT', 7860))
    demo.launch(server_name="0.0.0.0", server_port=port)
