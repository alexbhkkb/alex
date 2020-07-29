import base64
import json
import os
import re
import sys
import time

try:
	import aiocometd
	from py_mini_racer import py_mini_racer
	import requests
except ModuleNotFoundError:
	if "y" in input("Install dependencies? [y/N] > ").lower():
		os.system('python3 -m pip install -r requirements.txt')

import asyncio
from difflib import SequenceMatcher


class Kahoot:
	def __init__(self, pin, nickname, quizName=None, quizID=None, maxCount=None, DEBUG=None):
		self.pin = pin
		self.nickname = nickname
		self.quizName = quizName
		self.quizID = quizID
		self.client = requests.session()
		self.captchaToken = "KAHOOT_TOKEN_eyJ2ZXJzaW9uIjoiIn0="
		# This will work until Kahoot updates their code version (https://repl.it/repls/WholeCrimsonFlashdrives)
		self.authToken = None
		self.answers = None
		self.colors = {0: "RED", 1: "BLUE", 2: "YELLOW", 3: "GREEN"}
		self.maxCount = maxCount if maxCount else 50
		self.loadCodes()
		self.DEBUG = DEBUG;
		if not DEBUG:
			sys.tracebacklimit = 0

	def _check_auth(f):
		def wrapper(self, *args, **kwargs):
			if not self.authToken:
				raise KahootError('You must be authenticated to use this method.')
			return f(self, *args, **kwargs)

		return wrapper

	def authenticate(self, email, password):
		url = 'https://create.kahoot.it/rest/authenticate'
		data = {'username': email, 'password': password, 'grant_type': 'password'}
		response = self.client.post(url, json=data,
		                            headers={'Content-Type': 'application/json', "x-kahoot-login-gate": "enabled"})
		if response.status_code == 401:
			raise KahootError("Invalid Email or Password.")
		elif response.status_code == 200:
			print('AUTHENTICATED')
			self.authToken = response.json()["access_token"]
		else:
			raise KahootError("Login error %d", response.status_code)

	def startGame(self):
		loop = asyncio.get_event_loop()
		loop.run_until_complete(self._play())

	async def _play(self):
		url = f'wss://play.kahoot.it/cometd/{self.pin}/{self.sessionID}'
		async with aiocometd.Client(url, ssl=True) as client:
			self.socket = client
			await client.subscribe("/service/controller")
			await client.subscribe("/service/player")
			await client.subscribe("/service/status")
			await client.publish('/service/controller',
			                     {"host": "kahoot.it", "gameid": self.pin, "captchaToken": self.captchaToken,
			                      "name": self.nickname, "type": "login"})
			offset = 0
			tFADone = 0
			if self.quizID:
				self.answers = await self.findAnswers()
			async for rawMessage in client:
				message = rawMessage['data']
				if 'error' in message:
					raise KahootError(message['description'])
				if 'id' in message:
					data = json.loads(message['content'])
					kind = ''
					if message['id'] in self.lookup:
						kind = self.lookup[message['id']]
					if kind == 'TWO_FACTOR_AUTH_CORRECT':
						tFADone = True
					if kind == 'RESET_TWO_FACTOR_AUTH' and not tFADone:
						await self.submit2FA()
					if kind == 'START_QUIZ':
						if self.DEBUG:
							print(data)
						quizAnswers = data['quizQuestionAnswers']
						if not self.answers:
							self.answers = await self.findAnswers(exceptedAnswers=quizAnswers)
					elif kind == 'START_QUESTION':
						print('------', data['questionIndex'] + 1, '------')
						if data['gameBlockType'] != 'quiz':
							pass
						elif self.answers:
							correct = self.answers[data['questionIndex'] + offset]['index']
							print(f'SELECTED {self.colors[correct]}')
							await self.sendAnswer(correct)
						else:
							print('SELECTED FALLBACK')
							await self.sendAnswer(1)
					elif kind == 'TIME_UP':
						# print('DID NOT ANSWER IN TIME, SKIPPING TO NEXT ANSWER')
						# offset += 1
						pass
					elif kind == 'RESET_CONTROLLER':
						print("RESET_CONTROLLER")
						exit()
					elif kind == 'GAME_OVER':
						print("Game over, if you didn't win the winner is hacking!")
						exit()
					if not (tFADone and kind == 'RESET_TWO_FACTOR_AUTH'):
						print(kind.replace('_', ' '))

	def convert(self, cols):
		for num, color in self.colors.items():
			cols = cols.replace(color[0].lower(), str(num))
		return cols

	async def submit2FA(self):
		seq = self.convert(input("2fa (e.g. rbyg, yrgb) > ").lower())
		tfa = json.dumps({"sequence": seq})
		await self.socket.publish("/service/controller",
		                          {"content": tfa, "gameid": self.pin, "host": "kahoot.it", "type": "message",
		                           "id": 50})

	async def sendAnswer(self, choice):
		choiceInfo = json.dumps(
			{"choice": choice,
			 "meta": {"lag": 0, "device": {"userAgent": "kbot", "screen": {"width": 1920, "height": 1080}}}})
		await self.socket.publish("/service/controller",
		                          {"content": choiceInfo, "gameid": self.pin, "host": "kahoot.it", "type": "message",
		                           "id": 45})

	def printAnswers(self, resp, url):
		print("If the questions are randomized, go to " + url + "to get the answers yourself.")  # TODO: output answers

	async def getQuiz(self, url, exceptedAnswers=None, actualAnswers=None):
		if self.authToken:
			resp = self.client.get(url, headers={'Authorization': f'Bearer {self.authToken}'})
		else:
			resp = self.client.get(url)
		if resp.status_code == 400:
			raise KahootError("Invalid UUID.")
		if resp.status_code != 200:
			raise KahootError("Something went wrong finding answers.")
		if exceptedAnswers and actualAnswers:
			if actualAnswers == len(exceptedAnswers):
				isCorrectQuiz = True
				for q_index, question in enumerate(resp.json()['questions']):
					if len(question['choices']) != exceptedAnswers[q_index]:
						isCorrectQuiz = False
						break
				if isCorrectQuiz:
					print("QUIZ FOUND")
					self.printAnswers(resp.json(), url)
					return resp.json()
				else:
					print("Wrong question types")
			else:
				print("Wrong num of expected answers")
		else:
			print("No excepted answers")
			self.printAnswers(resp.json(), url)
			return resp.json()

	async def searchQuiz(self, exceptedAnswers=None):
		if self.quizID:
			url = f'https://create.kahoot.it/rest/kahoots/{self.quizID}'
			quiz = await self.getQuiz(url=url, exceptedAnswers=exceptedAnswers)
			return quiz
		elif self.quizName:
			url = 'https://create.kahoot.it/rest/kahoots/'
			params = {'query': self.quizName, 'cursor': 0, 'limit': self.maxCount, 'topics': '', 'grades': '',
			          'orderBy': 'relevance', 'searchCluster': 1, 'includeExtendedCounters': False}
			if self.DEBUG:
				print(self.authToken)
			if self.authToken:
				resp = self.client.get(url, params=params, headers={'Authorization': f'Bearer {self.authToken}'})
			else:
				resp = self.client.get(url, params=params)
			if resp.status_code != 200:
				raise KahootError("Something went wrong searching quizzes.")
			quizzes = resp.json()['entities']
			print(f'{len(quizzes)} matching quizzes found')
			for quiz in quizzes:
				print(f"Checking {quiz['card']['title']}...", end=" ")
				url = f'https://create.kahoot.it/rest/kahoots/{quiz["card"]["uuid"]}'
				rightQuiz = await self.getQuiz(url=url, exceptedAnswers=exceptedAnswers,
				                               actualAnswers=quiz['card']['number_of_questions'])
				if rightQuiz:
					return rightQuiz
			# Otherwise Panic
			raise KahootError("No quiz found. (private?)")

	@staticmethod
	def _remove_emojis(text):
		# https://stackoverflow.com/questions/33404752/removing-emojis-from-a-string-in-python/33417311
		emoji_pattern = re.compile("["
		                           u"\U0001F600-\U0001F64F"  # emoticons
		                           u"\U0001F300-\U0001F5FF"  # symbols & pictographs
		                           u"\U0001F680-\U0001F6FF"  # transport & map symbols
		                           u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
		                           "]+", flags=re.UNICODE)
		return emoji_pattern.sub(r'', text)

	@staticmethod
	def _similar(a, b):
		return SequenceMatcher(None, a, b).ratio()

	async def findAnswers(self, exceptedAnswers=None):
		quizProperties = await self.searchQuiz(exceptedAnswers)
		answers = []
		if self.DEBUG:
			print(quizProperties)
		for question in quizProperties['questions']:
			foundAnswer = False
			if question['type'] != 'quiz':
				answers.append({'NOT A': 'QUESTION'})
				continue
			for i, choice in enumerate(question['choices']):
				if choice['correct'] and not foundAnswer:
					foundAnswer = True
					answers.append({'question': question['question'], 'index': i, 'answer': choice['answer']})
		print(f'ANSWERS RECEIVED')
		return answers

	def checkPin(self):
		assert type(self.pin) == str
		currentTime = int(time.time())
		url = f"https://play.kahoot.it/reserve/session/{self.pin}/?{currentTime}"
		resp = self.client.get(url)
		if resp.status_code != 200:
			raise KahootError(f"Pin {self.pin} does not exist.")
		self.sessionToken = resp.headers['x-kahoot-session-token']
		self.sessionID = self.solveChallenge(resp.json()["challenge"])

	def solveChallenge(self, text):
		# Rebuilt Javascript so engine can solve it
		text = text.replace('\t', '', -1).encode('ascii', 'ignore').decode('utf-8')
		text = re.split("[{};]", text)
		replaceFunction = "return message.replace(/./g, function(char, position) {"
		rebuilt = [text[1] + "{", text[2] + ";", replaceFunction, text[7] + ";})};", text[0]]

		jsEngine = py_mini_racer.MiniRacer()
		solution = jsEngine.eval("".join(rebuilt))
		return self._shiftBits(solution)

	def _shiftBits(self, solution):
		decodedToken = base64.b64decode(self.sessionToken).decode('utf-8', 'strict')
		solChars = [ord(s) for s in solution]
		sessChars = [ord(s) for s in decodedToken]
		return "".join([chr(sessChars[i] ^ solChars[i % len(solChars)]) for i in range(len(sessChars))])

	def loadCodes(self):
		self.lookup = {
			1: "GET_READY",
			2: "START_QUESTION",
			3: "GAME_OVER",
			4: "TIME_UP",
			5: "PLAY_AGAIN",
			6: "ANSWER_SELECTED",
			7: "ANSWER_RESPONSE",
			8: "REVEAL_ANSWER",
			9: "START_QUIZ",
			10: "RESET_CONTROLLER",
			11: "SUBMIT_FEEDBACK",
			12: "FEEDBACK",
			13: "REVEAL_RANKING",
			14: "USERNAME_ACCEPTED",
			15: "USERNAME_REJECTED",
			16: "REQUEST_RECOVERY_DATA_FROM_PLAYER",
			17: "SEND_RECOVERY_DATA_TO_CONTROLLER",
			18: "JOIN_TEAM_MEMBERS",
			19: "JOIN_TEAM_MEMBERS_RESPONSE",
			20: "START_TEAM_TALK",
			21: "SKIP_TEAM_TALK",
			31: "IFRAME_CONTROLLER_EVENT",
			32: "SERVER_IFRAME_EVENT",
			40: "STORY_BLOCK_GET_READY",
			41: "REACTION_SELECTED",
			42: "REACTION_RESPONSE",
			43: "GAME_BLOCK_START",
			44: "GAME_BLOCK_END",
			45: "GAME_BLOCK_ANSWER",
			50: "SUBMIT_TWO_FACTOR",
			51: "TWO_FACTOR_AUTH_INCORRECT",
			52: "TWO_FACTOR_AUTH_CORRECT",
			53: "RESET_TWO_FACTOR_AUTH"
		}


class KahootError(Exception):
	pass
