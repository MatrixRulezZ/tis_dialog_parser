import requests
from bs4 import BeautifulSoup as BS
import telebot
from telebot import apihelper


# Коннектимся, парсим и получаем итоговые значения.
# Функция возвращает баланс, траффик, и скорость.
class Values:
    def __init__(self):
        url = 'https://stats.tis-dialog.ru/index.php'
        user_agent_val = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/75.0.3770.142 Safari/537.36'
        session = requests.Session()
        r = session.get(url, headers={
            'User-Agent': user_agent_val
        })
        session.headers.update({'Referer': url})
        session.headers.update({'User-Agent': user_agent_val})

        # Получаем значение _xsrf из cookies
        _xsrf = session.cookies.get('_xsrf', domain=".hh.ru")

        # Осуществляем вход с помощью метода POST с указанием необходимых данных
        post_request = session.post(url, {
            'backUrl': 'https://stats.tis-dialog.ru/index.php',
            'login': '2638920',
            'passv': '39361951',
            '_xsrf': _xsrf,
            'remember': 'yes',
        })
        post_request.encoding = "windows-1251"
        html = BS(post_request.content, 'html.parser')
        for a in html.findAll("a"):
            html.a.decompose()
        data_speed = html.select(".lkInfoTable:nth-child(3) > tr:nth-child(1) > td:nth-child(2) ")
        if len(data_speed) > 0:
            self.speed = data_speed[0].text
        else:
            print("Ошибка в количестве значений скорости соединения!")

        money_data = html.select(".lkInfoTable:nth-child(2) > tr:nth-child(4) > td:nth-child(2)")
        if len(money_data) > 0:
            self.money = money_data[0].text
        else:
            print("Ошибка в количестве значений баланса!")


        traffic_data = html.select(".lkInfoTable:nth-child(3) > tr:nth-child(2) > td:nth-child(2)")
        if len(traffic_data) > 0:
            traffic = traffic_data[0].text
            traffic = ''.join(i for i in traffic if not i.isalpha())
            traffic = traffic.replace(" ","")
            traffic = int(traffic)
            traffic = round(traffic/(1024**3),2)
            self.traffic = str(traffic)+" Гб"
        else:
            print("Ошибка в количестве значений баланса!")

def values_to_messaging():
    return Values()

def printer():
 x=values_to_messaging()
 print(x.traffic,x.money,x.speed)


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    printer()

# See PyCharm help at https://www.jetbrains.com/help/pycharm/
