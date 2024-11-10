from flask import Flask, request, send_file, render_template, jsonify
from werkzeug.utils import secure_filename
import os
import whisper
from openai import OpenAI
import requests
import json
from moviepy.editor import VideoFileClip, concatenate_videoclips, concatenate_audioclips
from moviepy.video.fx.all import resize
from tempfile import TemporaryDirectory
import threading

app = Flask(__name__)

# Configuration
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi'}

# Assurez-vous que le dossier uploads existe
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Charger le modèle Whisper une seule fois au démarrage
print("Loading Whisper model...")
model = whisper.load_model("tiny")
print("Whisper model loaded!")

# Configuration des clés API
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY')

# Dictionnaire pour stocker l'état du traitement
processing_status = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def split_array(arr, max_size=20):
    return [arr[i:i + max_size] for i in range(0, len(arr), max_size)]

def fetch_pexels_video(keyword, orientation="portrait"):
    url = f"https://api.pexels.com/videos/search?query={keyword}&orientation={orientation}&size=medium"
    headers = {"Authorization": PEXELS_API_KEY}
    response = requests.get(url, headers=headers)
    data = response.json()
    
    if data.get('total_results', 0) > 0:
        video_info = data['videos'][0]
        video_url = video_info['video_files'][0]['link']
        thumbnail_url = video_info['image']
        return {'video': video_url, 'thumbnail': thumbnail_url}
    return "Invalid keyword"

def process_video(video_path, task_id):
    try:
        processing_status[task_id] = {'status': 'processing', 'progress': 0}
        
        # 1. Extraire l'audio
        video = VideoFileClip(video_path)
        audio_path = f"{video_path}_audio.wav"
        video.audio.write_audiofile(audio_path)
        processing_status[task_id]['progress'] = 20

        # 2. Transcription avec Whisper
        result = model.transcribe(audio_path)
        segments = result["segments"]
        extracted_data = [{'start': item['start'], 'end': item['end'], 'text': item['text']} 
                         for item in segments]
        processing_status[task_id]['progress'] = 40

        # 3. Générer les mots-clés avec OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        data = [x["text"] for x in extracted_data]
        split_arrays = split_array(data, max_size=20)
        broll_info = []

        for i, x in enumerate(split_arrays):
            prompt = """Generate keywords for b-roll footage from these transcript segments..."""
            chat_completion = client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="gpt-4"
            )
            broll_data = json.loads(chat_completion.choices[0].message.content)
            broll_data = [{"k": x["k"], "i": 20*i+x["i"]} for x in broll_data]
            broll_info.extend(broll_data)
        
        processing_status[task_id]['progress'] = 60

        # 4. Télécharger et assembler les vidéos
        with TemporaryDirectory() as temp_dir:
            final_clips = []
            for segment in extracted_data:
                start = segment['start']
                end = segment['end']
                segment_duration = end - start
                original_clip = video.subclip(start, end)
                
                if random.random() < 0.5:  # 50% chance d'ajouter un b-roll
                    pexels_video = fetch_pexels_video(broll_info[len(final_clips)]["k"])
                    if pexels_video != "Invalid keyword":
                        b_roll_path = os.path.join(temp_dir, f"broll_{len(final_clips)}.mp4")
                        with open(b_roll_path, 'wb') as f:
                            f.write(requests.get(pexels_video['video']).content)
                        
                        b_roll_clip = VideoFileClip(b_roll_path)
                        b_roll_clip = resize(b_roll_clip, newsize=(video.w, video.h))
                        b_roll_clip = b_roll_clip.set_audio(video.audio.subclip(start, end))
                        final_clips.append(b_roll_clip)
                    else:
                        final_clips.append(original_clip)
                else:
                    final_clips.append(original_clip)

            processing_status[task_id]['progress'] = 80
            
            # Assembler la vidéo finale
            final_video = concatenate_videoclips(final_clips)
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], f"output_{task_id}.mp4")
            final_video.write_videofile(output_path, audio_codec='aac')
            
            # Nettoyer
            final_video.close()
            video.close()
            os.remove(audio_path)
            
            processing_status[task_id] = {'status': 'completed', 'progress': 100, 'output': output_path}
            
    except Exception as e:
        processing_status[task_id] = {'status': 'error', 'error': str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'video' not in request.files:
        return jsonify({'error': 'No video file'}), 400
    
    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        task_id = str(uuid.uuid4())
        threading.Thread(target=process_video, args=(filepath, task_id)).start()
        
        return jsonify({'task_id': task_id})
    
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/status/<task_id>')
def get_status(task_id):
    return jsonify(processing_status.get(task_id, {'status': 'not_found'}))

@app.route('/download/<task_id>')
def download_file(task_id):
    status = processing_status.get(task_id)
    if status and status['status'] == 'completed':
        return send_file(status['output'], as_attachment=True)
    return jsonify({'error': 'File not ready or not found'}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
