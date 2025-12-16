set -e  # Прерывать при ошибках

echo "=== Установка проекта ==="

apt update && apt upgrade -y

apt install -y python3-pip python3-venv nginx postgresql postgresql-contrib
apt install -y certbot python3-certbot-nginx

if ! id "appuser" &>/dev/null; then
    useradd -m -s /bin/bash appuser
    echo "Пользователь appuser создан"
fi

cd /opt
if [ -d "my_project" ]; then
    echo "Проект уже существует, обновляем..."
    cd my_project
    sudo -u appuser git pull origin main
else
    git clone https://github.com/yourusername/my_project.git
    chown -R appuser:appuser my_project
    cd my_project
fi

sudo -u appuser python3 -m venv venv
sudo -u appuser ./venv/bin/pip install --upgrade pip
sudo -u appuser ./venv/bin/pip install -r requirements.txt

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Создан файл .env. ОБЯЗАТЕЛЬНО отредактируйте его!"
    echo "Команда: sudo nano /opt/my_project/.env"
fi

cp deploy/backend.service /etc/systemd/system/
cp deploy/telegram_bot.service /etc/systemd/system/

sed -i "s|/opt/my_project|$(pwd)|g" /etc/systemd/system/backend.service
sed -i "s|/opt/my_project|$(pwd)|g" /etc/systemd/system/telegram_bot.service

systemctl daemon-reload

systemctl enable backend telegram_bot

echo "=== Установка завершена ==="
echo "Что сделать дальше:"
echo "1. Отредактировать файл .env: sudo nano /opt/my_project/.env"
echo "2. Настроить базу данных PostgreSQL"
echo "3. Настроить Nginx (см. инструкцию ниже)"
echo "4. Запустить сервисы: sudo systemctl start backend telegram_bot"