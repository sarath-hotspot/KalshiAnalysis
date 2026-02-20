

# Crontab
0 * * * * /bin/bash -c "source /home/sarath_hotspot/git/KalshiAnalysis/.venv/bin/activate && cd /home/sarath_hotspot/git/KalshiAnalysis/ && python3 ./live_trading.py --bet-contracts 1 --volume-min 100000 --volume-max 500000 --hours 3" >> /home/sarath_hotspot/git/KalshiAnalysis/trading.log 2>&1



