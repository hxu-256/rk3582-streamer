import cv2
import sys

DEVICE = "/dev/video11"

cap = cv2.VideoCapture(DEVICE)
if not cap.isOpened():
    print(f"Failed to open {DEVICE}")
    sys.exit(1)

print(f"Opened {DEVICE} — press q to quit")

while True:
    ret, frame = cap.read()
    if not ret:
        print("Failed to read frame")
        break

    cv2.imshow("Camera", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
