"""Process-safe file locking for Windows and POSIX; close/unlock on every exit."""
from contextlib import contextmanager
import os, time
from pathlib import Path
@contextmanager
def exclusive_lock(path):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
 with path.open('a+b') as stream:
  if os.name=='nt':
   import msvcrt
   stream.seek(0,2)
   if stream.tell()==0:stream.write(b'0');stream.flush()
   while True:
    try:
     stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
    except OSError as exc:
     if exc.errno not in (13,36):raise
     time.sleep(0.05)
  else:
   import fcntl
   fcntl.flock(stream,fcntl.LOCK_EX)
  try:yield stream
  finally:
   if os.name=='nt':stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
   else:fcntl.flock(stream,fcntl.LOCK_UN)
