import yt_dlp

url = input("Enter the YouTube video URL: ")

yt_dlp.YoutubeDL({"format": "bestvideo+bestaudio/best"}).download([url])
