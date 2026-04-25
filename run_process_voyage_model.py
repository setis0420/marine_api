"""
process_voyage_model.py 실행 런처 (GPU 환경)
- kki_gpu2 conda 환경의 python으로 process_voyage_model.py를 실행
- 전달받은 인자는 그대로 넘김 (--mmsi, --workers 등)
"""
import sys
import subprocess

GPU_PYTHON = r"C:\Users\user\anaconda3\envs\kki_gpu2\python.exe"
TARGET = r"k:\coding_project\해양수산 데이터 분석 플랫폼\fishing_model\process_voyage_model.py"

if __name__ == '__main__':
    cmd = [GPU_PYTHON, TARGET] + sys.argv[1:]
    sys.exit(subprocess.call(cmd))
