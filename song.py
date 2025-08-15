import gradio as gr
import pandas as pd
import os
import subprocess
import re
import time
from pathlib import Path
import shutil
import zipfile
import uuid

# --- ヘルパー関数 ---

def sanitize_filename(name):
    """ファイル名として使えない文字を安全な文字に置換します。"""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

# --- Gradio アプリケーションのコアロジック ---

def load_csv_and_prepare(uploaded_file):
    """
    CSVファイルを読み込み、処理の準備をします。
    Stateを更新し、UIコンポーネントの表示/非表示を制御します。
    """
    if uploaded_file is None:
        return None, 0, "エラー: CSVファイルをアップロードしてください。", gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    file_path = uploaded_file.name
    data = []
    try:
        # Shift-JIS (cp932) でファイルを読み込み
        with open(file_path, 'r', encoding='cp932') as f:
            next(f)  # ヘッダーをスキップ
            for line in f:
                parts = line.strip().split(',', 1)
                if len(parts) == 2:
                    song_title, artist_name = parts
                    data.append({'曲名': song_title.strip(), 'バンド名': artist_name.strip()})
    except Exception as e:
        return None, 0, f"ファイルの読み込み中にエラーが発生しました: {e}", gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    df = pd.DataFrame(data)
    total_songs = len(df)
    log_message = f"CSVファイルを読み込みました。合計 {total_songs} 曲。\n最初の20曲のダウンロード準備ができました。"
    
    # UIの表示を更新
    return df, 0, log_message, gr.update(visible=True, interactive=True), gr.update(visible=False), gr.update(visible=True)


def download_batch(song_df, current_index, progress=gr.Progress(track_tqdm=True)):
    """
    現在のインデックスから20曲をダウンロードし、ZIPファイルを作成します。
    """
    if song_df is None or song_df.empty:
        yield "エラー: 曲リストが読み込まれていません。", None, gr.update(interactive=False)
        return

    # セッションごとにユニークな一時ディレクトリを作成
    session_id = str(uuid.uuid4())
    batch_dir = Path(f"/tmp/music_batch_{session_id}")
    batch_dir.mkdir(exist_ok=True)

    start_index = current_index
    end_index = min(start_index + 20, len(song_df))
    
    # このバッチで処理する曲のDataFrameスライスを作成
    batch_df = song_df.iloc[start_index:end_index]
    
    log = f"バッチ {start_index//20 + 1} の処理を開始します ({start_index + 1}曲目から{end_index}曲目まで)。\n"
    yield log, None, gr.update(interactive=False) # ダウンロード中はボタンを無効化

    for index, row in progress.tqdm(batch_df.iterrows(), total=len(batch_df), desc=f"バッチ {start_index//20 + 1} をダウンロード中"):
        song_title = row['曲名']
        artist_name = row['バンド名']
        safe_song_title = sanitize_filename(f"{artist_name} - {song_title}")

        log += f"  > {safe_song_title} をダウンロード中..."
        yield log, None, gr.update(interactive=False)

        output_template = str(batch_dir / safe_song_title)
        search_query = f"ytsearch1:{artist_name} {song_title} audio"
        
        command = [
            'yt-dlp', '--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0',
            '--output', f"{output_template}.%(ext)s",
            '--ignore-errors', '--no-playlist', '--quiet', '--no-warnings',
            search_query
        ]

        try:
            subprocess.run(command, check=True, capture_output=True, text=True, encoding='utf-8')
            log += " ✔ 成功\n"
        except subprocess.CalledProcessError as e:
            log += f" ❌ 失敗\n    エラー: {e.stderr.strip()}\n"
        
        yield log, None, gr.update(interactive=False)

    # ZIPファイルを作成
    zip_path = f"/tmp/music_batch_{start_index//20 + 1}.zip"
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for file in batch_dir.glob('*.mp3'):
            zipf.write(file, file.name)
    
    # 一時ファイルをクリーンアップ
    shutil.rmtree(batch_dir)

    new_index = end_index
    is_last_batch = new_index >= len(song_df)
    
    log += f"\nバッチ {start_index//20 + 1} の処理が完了しました。\nZIPファイルをダウンロードできます。"
    if not is_last_batch:
        log += f"\n準備ができたら「次の20曲をダウンロード」ボタンを押してください。"
    else:
        log += f"\nすべての曲の処理が完了しました。"

    # ZIPファイルのダウンロードボタンを表示し、次のバッチボタンを更新
    yield log, gr.update(value=zip_path, visible=True), gr.update(interactive=not is_last_batch)
    

def delete_all_data():
    """/tmp に作成された関連ファイルをすべて削除します。"""
    deleted_files = []
    deleted_dirs = []
    for path in Path("/tmp").glob("music_batch_*"):
        if path.is_file() and path.suffix == '.zip':
            path.unlink()
            deleted_files.append(path.name)
        elif path.is_dir():
            shutil.rmtree(path)
            deleted_dirs.append(path.name)
            
    return f"クリーンアップ完了。\n削除したZIPファイル: {deleted_files}\n削除した一時フォルダ: {deleted_dirs}"


# --- Gradio UIの構築 ---

with gr.Blocks(theme=gr.themes.Soft()) as demo:
    # 状態を管理するための変数
    song_data = gr.State(None) # pd.DataFrameを保持
    current_index = gr.State(0) # 現在の処理開始インデックス

    gr.Markdown("# 🎵 YouTube Music Downloader for Hugging Face")
    gr.Markdown("CSVファイルをアップロードし、20曲ずつのバッチでMP3をダウンロードします。")

    with gr.Row():
        with gr.Column(scale=1):
            file_input = gr.File(label="CSVファイルをアップロード (cp932/Shift-JIS)", file_types=[".csv"])
            prepare_button = gr.Button("1. CSVを読み込む", variant="secondary")
            
            download_button = gr.Button("2. 曲のダウンロードを開始/次の20曲をダウンロード", variant="primary", visible=False)
            
            zip_download_component = gr.File(label="ZIPファイルをダウンロード", visible=False)

            cleanup_button = gr.Button("ダウンロードデータを全て削除", variant="stop", visible=False)

        with gr.Column(scale=2):
            progress_output = gr.Textbox(label="進捗状況", lines=15, interactive=False, autoscroll=True)

    # --- ボタンのアクションを定義 ---

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
        # download_batchが完了した後にインデックスを更新
        fn=lambda idx: idx + 20,
        inputs=current_index,
        outputs=current_index
    )

    cleanup_button.click(
        fn=delete_all_data,
        inputs=[],
        outputs=[progress_output]
    )

if __name__ == "__main__":
    demo.launch()
