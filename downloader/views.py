import yt_dlp
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.http import StreamingHttpResponse, FileResponse, HttpResponseRedirect
import re
import os
import tempfile
import urllib.request
import urllib.parse


def detect_platform(url):
    """Detect which platform a URL belongs to."""
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    elif 'facebook.com' in url_lower or 'fb.watch' in url_lower or 'fb.com' in url_lower:
        return 'facebook'
    return 'unknown'


def get_cookie_file():
    """
    Find or create a cookies.txt file for yt-dlp.
    1. Check for a local 'cookies.txt' in the project root.
    2. Check for 'YOUTUBE_COOKIES' environment variable and write to temp file if found.
    """
    local_cookies = os.path.join(os.getcwd(), 'cookies.txt')
    if os.path.exists(local_cookies):
        print("DEBUG: Using local cookies.txt")
        return local_cookies

    env_cookies = os.getenv('YOUTUBE_COOKIES')
    if env_cookies:
        # Render/production environment: write cookies from env var to a temp file
        try:
            temp_cookie_path = os.path.join(tempfile.gettempdir(), 'yt_cookies.txt')
            with open(temp_cookie_path, 'w', encoding='utf-8') as f:
                f.write(env_cookies)
            print("DEBUG: Using cookies from environment variable")
            return temp_cookie_path
        except Exception as e:
            print(f"DEBUG: Failed to write env cookies to temp file: {e}")
    else:
        print("DEBUG: YOUTUBE_COOKIES environment variable is MISSING")
    
    return None

def sanitize_url(url):
    """Basic URL validation and sanitization."""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url


class FetchInfoView(APIView):
    """
    POST /api/fetch-info/
    Accepts a URL and returns media metadata using yt-dlp.
    """

    def post(self, request):
        print(f"DEBUG: Received request with data: {request.data}")
        url = request.data.get('url', '').strip()

        if not url:
            return Response(
                {'error': 'URL is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        url = sanitize_url(url)
        platform = detect_platform(url)

        if platform == 'unknown':
            return Response(
                {'error': 'Unsupported platform. Please use YouTube, Instagram, or Facebook URLs.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True,
            'socket_timeout': 15,
            'cookiefile': get_cookie_file(),
            'nocheckcertificate': True,
            'check_formats': False,  # Bypasses the "Requested format is not available" error on cloud IPs
            'format': 'best',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Referer': 'https://www.youtube.com/',
                'Sec-Fetch-Mode': 'navigate',
            }
        }



        # Check for node to use as JS runtime (needed for some YouTube signatures)
        import shutil
        if shutil.which('node'):
            ydl_opts['js_runtimes'] = {'node': {}}


        # For Instagram, we may need cookies or specific options
        if platform == 'instagram':
            ydl_opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            }

        try:
            print(f"DEBUG: Starting extraction for URL: {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                print("DEBUG: extraction failed: NO INFO")
                return Response(
                    {'error': 'Could not extract media information from the provided URL.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            print(f"DEBUG: Successfully extracted info for: {info.get('title')}")
            # Process formats
            formats = []
            seen_qualities = set()

            raw_formats = info.get('formats', [])
            print(f"DEBUG: Found {len(raw_formats)} raw formats")

            for f in raw_formats:
                format_id = f.get('format_id', '')
                ext = f.get('ext', 'mp4')
                height = f.get('height')
                width = f.get('width')
                filesize = f.get('filesize') or f.get('filesize_approx')
                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                tbr = f.get('tbr', 0)

                # Determine if this is video or audio
                has_video = vcodec != 'none' and vcodec is not None
                has_audio = acodec != 'none' and acodec is not None

                if has_video and height:
                    quality = f'{height}p'
                    media_type = 'video'
                elif has_audio and not has_video:
                    abr = f.get('abr', tbr)
                    quality = f'{int(abr)}kbps' if abr else 'Audio'
                    media_type = 'audio'
                else:
                    continue

                quality_key = f'{media_type}_{quality}_{ext}'
                if quality_key in seen_qualities:
                    continue
                seen_qualities.add(quality_key)

                formats.append({
                    'format_id': format_id,
                    'quality': quality,
                    'ext': ext,
                    'filesize': filesize,
                    'type': media_type,
                    'has_audio': has_audio,
                    'has_video': has_video,
                    'height': height,
                    'tbr': tbr,
                })

            # Sort: video by height descending, audio by tbr descending
            video_formats = sorted(
                [f for f in formats if f['type'] == 'video'],
                key=lambda x: (x.get('height') or 0, x.get('tbr') or 0),
                reverse=True
            )
            audio_formats = sorted(
                [f for f in formats if f['type'] == 'audio'],
                key=lambda x: x.get('tbr') or 0,
                reverse=True
            )

            # Limit to reasonable number
            video_formats = video_formats[:8]
            audio_formats = audio_formats[:5]

            # If no formats found, add a "best" fallback
            if not video_formats and not audio_formats:
                print("DEBUG: No specific formats found, adding fallback")
                video_formats = [{
                    'format_id': 'best',
                    'quality': 'Best Available',
                    'ext': 'mp4',
                    'filesize': None,
                    'type': 'video',
                    'has_audio': True,
                    'has_video': True,
                    'height': None,
                    'tbr': 0,
                }]

            response_data = {
                'title': info.get('title', 'Unknown Title'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration'),
                'uploader': info.get('uploader') or info.get('channel', ''),
                'platform': platform,
                'url': url,
                'formats': video_formats + audio_formats,
                'description': (info.get('description', '') or '')[:200],
            }

            print("DEBUG: Returning successful response")
            return Response(response_data, status=status.HTTP_200_OK)

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            print(f"DEBUG: yt-dlp DownloadError: {error_msg}")
            if 'Private video' in error_msg:
                return Response(
                    {'error': 'This video is private and cannot be accessed.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            elif 'Video unavailable' in error_msg:
                return Response(
                    {'error': 'This video is unavailable or has been removed.'},
                    status=status.HTTP_404_NOT_FOUND
                )
            elif 'Sign in to confirm you\'re not a bot' in error_msg:
                return Response(
                    {'error': 'YouTube is detecting a bot. Please set the YOUTUBE_COOKIES environment variable.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            return Response(
                {'error': f'Failed to fetch media (yt-dlp): {error_msg[:200]}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            print(f"DEBUG: Unexpected Exception: {str(e)}")
            return Response(
                {'error': f'An unexpected error occurred in backend: {str(e)[:200]}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )



class DownloadView(APIView):
    """
    GET /api/download/?url=...&format_id=...&quality=...&media_type=...
    Downloads the media via yt-dlp to a temp file, then streams it to the user
    with Content-Disposition: attachment to force a real file download.
    """

    def get(self, request):
        url = request.query_params.get('url', '').strip()
        format_id = request.query_params.get('format_id', 'best').strip()
        quality = request.query_params.get('quality', '').strip()
        media_type = request.query_params.get('media_type', 'video').strip()

        if not url:
            return Response(
                {'error': 'URL is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        url = sanitize_url(url)
        platform = detect_platform(url)

        # Create a temp directory for the download
        tmp_dir = tempfile.mkdtemp()
        output_template = os.path.join(tmp_dir, '%(title).80s.%(ext)s')

        # Build format string based on quality and media type
        # Using quality-based selection instead of raw format_ids to avoid 403 errors
        fmt = self._build_format_string(format_id, quality, media_type, platform)

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'format': fmt,
            'outtmpl': output_template,
            'merge_output_format': 'mp4' if media_type == 'video' else None,
            'socket_timeout': 30,
            'retries': 5,
            'fragment_retries': 5,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Sec-Fetch-Mode': 'navigate',
            },
            'nocheckcertificate': True,
            'remote_components': 'ejs:github',
            'cookiefile': get_cookie_file(),
        }

        # Check for node to use as JS runtime
        import shutil
        if shutil.which('node'):
            ydl_opts['js_runtimes'] = {'node': {}}


        # Remove None values
        ydl_opts = {k: v for k, v in ydl_opts.items() if v is not None}

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if not info:
                self._cleanup_tmp(tmp_dir)
                return Response(
                    {'error': 'Could not download the media.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Find the downloaded file (pick the largest if multiple)
            downloaded_file = None
            max_size = 0
            for f in os.listdir(tmp_dir):
                filepath = os.path.join(tmp_dir, f)
                if os.path.isfile(filepath):
                    fsize = os.path.getsize(filepath)
                    if fsize > max_size:
                        max_size = fsize
                        downloaded_file = filepath

            if not downloaded_file or not os.path.exists(downloaded_file):
                self._cleanup_tmp(tmp_dir)
                return Response(
                    {'error': 'Download completed but file not found.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Get file info
            title = info.get('title', 'download')
            ext = os.path.splitext(downloaded_file)[1].lstrip('.')
            if not ext:
                ext = 'mp4' if media_type == 'video' else 'm4a'
            safe_title = re.sub(r'[^\w\s\-.]', '', title)[:100].strip()
            if not safe_title:
                safe_title = 'download'
            filename = f"{safe_title}.{ext}"

            # Stream the file back
            file_size = os.path.getsize(downloaded_file)

            def file_iterator(file_path, chunk_size=65536):
                try:
                    with open(file_path, 'rb') as fh:
                        while True:
                            chunk = fh.read(chunk_size)
                            if not chunk:
                                break
                            yield chunk
                finally:
                    try:
                        os.remove(file_path)
                        os.rmdir(tmp_dir)
                    except Exception:
                        pass

            # Determine content type
            content_types = {
                'mp4': 'video/mp4',
                'webm': 'video/webm',
                'mkv': 'video/x-matroska',
                'mp3': 'audio/mpeg',
                'm4a': 'audio/mp4',
                'wav': 'audio/wav',
                'ogg': 'audio/ogg',
                'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg',
                'png': 'image/png',
                'webp': 'image/webp',
            }
            content_type = content_types.get(ext.lower(), 'application/octet-stream')

            response = StreamingHttpResponse(
                file_iterator(downloaded_file),
                content_type=content_type,
            )
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            response['Content-Length'] = file_size
            response['Access-Control-Expose-Headers'] = 'Content-Disposition'

            return response

        except yt_dlp.utils.DownloadError as e:
            self._cleanup_tmp(tmp_dir)
            return Response(
                {'error': f'Download failed: {str(e)[:200]}'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            self._cleanup_tmp(tmp_dir)
            return Response(
                {'error': f'Download failed: {str(e)[:200]}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _build_format_string(self, format_id, quality, media_type, platform):
        """
        Build a proper yt-dlp format selection string.
        - If ffmpeg is available: use bestvideo+bestaudio (merge) for highest quality
        - If ffmpeg is NOT available: use 'best' (single pre-merged stream, max ~720p on YT)
        """
        import shutil
        has_ffmpeg = shutil.which('ffmpeg') is not None

        if media_type == 'audio':
            return 'bestaudio[ext=m4a]/bestaudio/best'

        # Extract height from quality string (e.g., "720p" -> 720)
        height = None
        if quality:
            try:
                height = int(quality.replace('p', '').replace('k', '').strip())
            except (ValueError, AttributeError):
                height = None

        if has_ffmpeg:
            # FFmpeg available — can merge separate video+audio streams
            if height:
                return (
                    f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/'
                    f'bestvideo[height<={height}]+bestaudio/'
                    f'best[height<={height}]/'
                    f'best'
                )
            else:
                return 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        else:
            # No FFmpeg — use single pre-merged streams only (no merge needed)
            if height:
                return (
                    f'best[height<={height}][ext=mp4]/'
                    f'best[height<={height}]/'
                    f'best[ext=mp4]/'
                    f'best'
                )
            else:
                return 'best[ext=mp4]/best'

    def _cleanup_tmp(self, tmp_dir):
        """Clean up temp directory and its contents."""
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except Exception:
            pass


