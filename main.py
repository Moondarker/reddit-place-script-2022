import math
import random

import requests
import json
import time
import threading
import pickle
import os
import sys
from io import BytesIO
from http import HTTPStatus
from websocket import create_connection
from websocket import _exceptions as WSExceptions
from PIL import Image

from loguru import logger
import click
from bs4 import BeautifulSoup


from src.mappings import ColorMapper
import src.proxy as proxy
import src.utils as utils


class PlaceClient:
    def __init__(self, dry, config_path):
        self.logger = logger
        # Data
        self.json_data = utils.get_json_data(self, config_path)
        self.pixel_x_start: int = self.json_data["image_start_coords"][0]
        self.pixel_y_start: int = self.json_data["image_start_coords"][1]
        self.board = None
        self.board_expires_at = 0

        self.dry = dry

        # In seconds
        self.delay_between_launches = (
            self.json_data["thread_delay"]
            if "thread_delay" in self.json_data
            and self.json_data["thread_delay"] is not None
            else 3
        )
        self.unverified_place_frequency = (
            self.json_data["unverified_place_frequency"]
            if "unverified_place_frequency" in self.json_data
            and self.json_data["unverified_place_frequency"] is not None
            else False
        )

        self.legacy_transparency = (
            self.json_data["legacy_transparency"]
            if "legacy_transparency" in self.json_data
            and self.json_data["legacy_transparency"] is not None
            else True
        )
        proxy.Init(self)

        # Color palette
        self.rgb_colors_array = ColorMapper.generate_rgb_colors_array()

        # Auth
        self.access_tokens = {}
        if os.path.exists('./secrets.bin'):
            with open('secrets.bin', 'rb') as secrets:
                self.access_tokens = pickle.load(secrets)

        # Image information
        self.pix = None
        self.image_size = None
        self.image_path = (
            self.json_data["image_path"]
            if "image_path" in self.json_data
            else "image.jpg"
        )
        self.first_run_counter = 0

        # Initialize-functions
        utils.load_image(self)

    """ Main """
    # Obtain access token

    def refresh_token(self, index, name, worker):
        current_timestamp = math.floor(time.time())

        if (
            len(self.access_tokens) == 0 or
            # index in self.access_tokens
            index not in self.access_tokens or
            (
                self.access_tokens.get(index)[1] and
                current_timestamp >=
                self.access_tokens.get(index)[1]
            )
        ):
            if not self.compactlogging:
                logger.info("Worker #{} :: Refreshing access token", index)

            # developer's reddit username and password
            try:
                username = name
                password = worker["password"]
                # note: use https://www.reddit.com/prefs/apps
            except Exception:
                logger.info(
                    "You need to provide all required fields to worker '{}'",
                    name,
                )
                exit(1)

            client = requests.Session()
            r = client.get("https://www.reddit.com/login")
            login_get_soup = BeautifulSoup(r.content, "html.parser")
            csrf_token = login_get_soup.find("input", {"name": "csrf_token"})[
                "value"
            ]
            data = {
                "username": username,
                "password": password,
                "dest": "https://www.reddit.com/",
                "csrf_token": csrf_token,
            }

            r = client.post(
                "https://www.reddit.com/login",
                data=data,
                proxies=proxy.get_random_proxy(self),
            )
            if r.status_code != 200:
                print("Authorization failed!")  # password is probably invalid
                return
            else:
                print("Authorization successful!")
            print("Obtaining access token...")
            r = client.get("https://www.reddit.com/")
            data_str = (
                BeautifulSoup(r.content, features="html.parser")
                .find("script", {"id": "data"})
                .contents[0][len("window.__r = "): -1]
            )
            data = json.loads(data_str)
            response_data = data["user"]["session"]

            if "error" in response_data:
                logger.info(
                    "An error occured. Make sure you have the correct credentials. Response data: {}",
                    response_data,
                )
                exit(1)

            self.access_tokens[index] = (response_data["accessToken"], (int(response_data["expiresIn"]) + current_timestamp))

            if not self.compactlogging:
                logger.info(
                    "Received new access token: {}************",
                    self.access_tokens.get(index)[0][:5],
                )

    def refresh_tokens(self, reset=False):
        id_names = list(enumerate(self.json_data["workers"]))

        if reset:
            self.access_tokens = {}

        for index, name in id_names:
            worker = self.json_data["workers"][name]
            self.refresh_token(index, name, worker)
        
        with open('secrets.bin', 'wb') as secrets:
            pickle.dump(self.access_tokens, secrets)

    # Draw a pixel at an x, y coordinate in r/place with a specific color

    def set_pixel_and_check_ratelimit(
        self,
        access_token_in,
        x,
        y,
        name,
        color_index_in=18,
        canvas_index=0,
        thread_index=-1,
    ):
        # canvas structure:
        # 0 | 1
        # 2 | 3
        success = False

        logger.warning(
            "Worker #{} - {}: Attempting to place {} pixel at {}, {}",
            thread_index,
            name,
            ColorMapper.color_id_to_name(color_index_in),
            x,
            y,
        )

        url = "https://gql-realtime-2.reddit.com/query"

        payload = json.dumps(
            {
                "operationName": "setPixel",
                "variables": {
                    "input": {
                        "actionName": "r/replace:set_pixel",
                        "PixelMessageData": {
                            "coordinate": {"x": x % 1000, "y": y % 1000},
                            "colorIndex": color_index_in,
                            "canvasIndex": math.floor(x / 1000) + (math.floor(y / 1000) * 2),
                        },
                    }
                },
                "query": "mutation setPixel($input: ActInput!) {\n  act(input: $input) {\n    data {\n      ... on BasicMessage {\n        id\n        data {\n          ... on GetUserCooldownResponseMessageData {\n            nextAvailablePixelTimestamp\n            __typename\n          }\n          ... on SetPixelResponseMessageData {\n            timestamp\n            __typename\n          }\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n",
            }
        )
        headers = {
            "origin": "https://hot-potato.reddit.com",
            "referer": "https://hot-potato.reddit.com/",
            "apollographql-client-name": "mona-lisa",
            "Authorization": "Bearer " + access_token_in,
            "Content-Type": "application/json",
        }

        if self.dry:
            return 3600, True

        response = requests.request(
            "POST",
            url,
            headers=headers,
            data=payload,
            proxies=proxy.get_random_proxy(self),
        )
        logger.debug(
            "Worker #{} - {}: Received response: {}", thread_index, name, response.text
        )

        # There are 2 different JSON keys for responses to get the next timestamp.
        # If we don't get data, it means we've been rate limited.
        # If we do, a pixel has been successfully placed.
        if response.json()["data"] is None:
            logger.debug(response.json().get("errors"))
            waitTime = math.floor(
                response.json()["errors"][0]["extensions"]["nextAvailablePixelTs"]
            )
            logger.error(
                "Worker #{} - {}: Failed placing pixel: rate limited",
                thread_index,
                name,
            )
        else:
            waitTime = math.floor(
                response.json()["data"]["act"]["data"][0]["data"][
                    "nextAvailablePixelTimestamp"
                ]
            )
            logger.success(
                "Worker #{} - {}: Succeeded placing pixel", thread_index, name
            )
            success = True

        # THIS COMMENTED CODE LETS YOU DEBUG THREADS FOR TESTING
        # Works perfect with one thread.
        # With multiple threads, every time you press Enter you move to the next one.
        # Move the code anywhere you want, I put it here to inspect the API responses.

        # Reddit returns time in ms and we need seconds, so divide by 1000
        return waitTime / 1000, success

    def get_board(self, access_token_in):
        logger.debug("Connecting and obtaining board images")
        while True:
            try:
                ws = create_connection(
                    "wss://gql-realtime-2.reddit.com/query",
                    origin="https://hot-potato.reddit.com",
                )
                break
            except Exception:
                logger.error(
                    "Failed to connect to websocket, trying again in 30 seconds..."
                )
                time.sleep(30)

        ws.send(
            json.dumps(
                {
                    "type": "connection_init",
                    "payload": {"Authorization": "Bearer " + access_token_in},
                }
            )
        )
        while True:
            try:
                msg = ws.recv()
                if msg is None:
                    logger.error("Reddit failed to acknowledge connection_init")
                    exit()
                if msg.startswith('{"type":"connection_ack"}'):
                    logger.debug("Connected to WebSocket server")
                    break
            except WSExceptions.WebSocketConnectionClosedException as e:
                logger.error("WebSocket connection dropped abruptly. Checking saved tokens.")
                self.refresh_tokens()
                return self.get_board(random.choice(self.access_tokens)[0])

        logger.debug("Obtaining Canvas information")
        ws.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "start",
                    "payload": {
                        "variables": {
                            "input": {
                                "channel": {
                                    "teamOwner": "AFD2022",
                                    "category": "CONFIG",
                                }
                            }
                        },
                        "extensions": {},
                        "operationName": "configuration",
                        "query": "subscription configuration($input: SubscribeInput!) {\n  subscribe(input: $input) {\n    id\n    ... on BasicMessage {\n      data {\n        __typename\n        ... on ConfigurationMessageData {\n          colorPalette {\n            colors {\n              hex\n              index\n              __typename\n            }\n            __typename\n          }\n          canvasConfigurations {\n            index\n            dx\n            dy\n            __typename\n          }\n          canvasWidth\n          canvasHeight\n          __typename\n        }\n      }\n      __typename\n    }\n    __typename\n  }\n}\n",
                    },
                }
            )
        )

        while True:
            canvas_payload = json.loads(ws.recv())
            if canvas_payload["type"] == "data":
                canvas_details = canvas_payload["payload"]["data"]["subscribe"]["data"]
                logger.debug("Canvas config: {}", canvas_payload)
                break

        canvas_sockets = []

        canvas_count = len(canvas_details["canvasConfigurations"])

        for i in range(0, canvas_count):
            canvas_sockets.append(2 + i)
            logger.debug("Creating canvas socket {}", canvas_sockets[i])

            ws.send(
                json.dumps(
                    {
                        "id": str(2 + i),
                        "type": "start",
                        "payload": {
                            "variables": {
                                "input": {
                                    "channel": {
                                        "teamOwner": "AFD2022",
                                        "category": "CANVAS",
                                        "tag": str(i),
                                    }
                                }
                            },
                            "extensions": {},
                            "operationName": "replace",
                            "query": "subscription replace($input: SubscribeInput!) {\n  subscribe(input: $input) {\n    id\n    ... on BasicMessage {\n      data {\n        __typename\n        ... on FullFrameMessageData {\n          __typename\n          name\n          timestamp\n        }\n        ... on DiffFrameMessageData {\n          __typename\n          name\n          currentTimestamp\n          previousTimestamp\n        }\n      }\n      __typename\n    }\n    __typename\n  }\n}\n",
                        },
                    }
                )
            )

        imgs = []
        logger.debug("A total of {} canvas sockets opened", len(canvas_sockets))

        while len(canvas_sockets) > 0:
            temp = json.loads(ws.recv())
            logger.debug("Waiting for WebSocket message")

            if temp["type"] == "data":
                logger.debug("Received WebSocket data type message")
                msg = temp["payload"]["data"]["subscribe"]

                if msg["data"]["__typename"] == "FullFrameMessageData":
                    logger.debug("Received full frame message")
                    img_id = int(temp["id"])
                    logger.debug("Image ID: {}", img_id)

                    if img_id in canvas_sockets:
                        logger.debug("Getting image: {}", msg["data"]["name"])
                        imgs.append(
                            [
                                img_id,
                                Image.open(
                                    BytesIO(
                                        requests.get(
                                            msg["data"]["name"],
                                            stream=True,
                                            proxies=proxy.get_random_proxy(self),
                                        ).content
                                    )
                                ),
                            ]
                        )
                        canvas_sockets.remove(img_id)
                        logger.debug(
                            "Canvas sockets remaining: {}", len(canvas_sockets)
                        )

        for i in range(0, canvas_count - 1):
            ws.send(json.dumps({"id": str(2 + i), "type": "stop"}))

        ws.close()

        new_img_width = (
            max(map(lambda x: x["dx"], canvas_details["canvasConfigurations"]))
            + canvas_details["canvasWidth"]
        )
        logger.debug("New image width: {}", new_img_width)

        new_img_height = (
            max(map(lambda x: x["dy"], canvas_details["canvasConfigurations"]))
            + canvas_details["canvasHeight"]
        )
        logger.debug("New image height: {}", new_img_height)

        new_img = Image.new("RGB", (new_img_width, new_img_height))

        for idx, img in enumerate(sorted(imgs, key=lambda x: x[0])):
            logger.debug("Adding image (ID {}): {}", img[0], img[1])
            dx_offset = int(canvas_details["canvasConfigurations"][idx]["dx"])
            dy_offset = int(canvas_details["canvasConfigurations"][idx]["dy"])
            new_img.paste(img[1], (dx_offset, dy_offset))

        return new_img

    def get_unset_pixels(self, access_token_in):
        result = []
        x = 0
        y = 0

        if time.time() >= self.board_expires_at:
            logger.info('Updating map')
            boardimg = self.get_board(access_token_in)
            self.board = boardimg.convert("RGB").load()
            self.board_expires_at = time.time() + 30

        for y in range(self.image_size[1]):
            for x in range(self.image_size[0]):
                logger.debug("ABS X{} Y{}", x + self.pixel_x_start, y + self.pixel_y_start)
                logger.debug(
                    "REL X{}/{} Y{}/{}", x, self.image_size[0], y, self.image_size[1]
                )

                target_rgb = self.pix[x, y]

                new_rgb = ColorMapper.closest_color(
                    target_rgb, self.rgb_colors_array, self.legacy_transparency
                )
                if self.board[x + self.pixel_x_start, y + self.pixel_y_start] != new_rgb:
                    logger.debug(
                        "{}, {}, {}, {}",
                        self.board[x + self.pixel_x_start, y + self.pixel_y_start],
                        new_rgb,
                        new_rgb != (69, 42, 0),
                        self.board[x, y] != new_rgb,
                    )

                    # (69, 42, 0) is a special color reserved for transparency.
                    if target_rgb[3] == 0 or new_rgb == (69, 42, 0):
                        logger.trace(
                            "Transparent Pixel at {}, {} skipped",
                            x + self.pixel_x_start,
                            y + self.pixel_y_start,
                        )
                    else:
                        logger.debug(
                            "Replacing {} pixel at: {},{} with {} color",
                            self.board[x + self.pixel_x_start, y + self.pixel_y_start],
                            x + self.pixel_x_start,
                            y + self.pixel_y_start,
                            new_rgb,
                        )
                        result.append((x, y, new_rgb))
                        break

        return result

    # Draw the input image
    def main_thread(self, workers):
        id_names = list(enumerate(workers))

        if self.unverified_place_frequency:
            pixel_place_frequency = 1230
        else:
            pixel_place_frequency = 330

        for index, name in id_names:
            worker = workers[name]

            worker["active"] = True
            worker["next_draw_time"] = math.floor(time.time()) + pixel_place_frequency

            self.refresh_token(index, name, worker)

            try:
                # Current pixel row and pixel column being drawn
                current_r = worker["start_coords"][0]
                current_c = worker["start_coords"][1]
            except Exception:
                logger.info("You need to provide start_coords to worker '{}'", name)
                exit(1)
        
        with open('secrets.bin', 'wb') as secrets:
            pickle.dump(self.access_tokens, secrets)

        while True:
            targets: list = self.get_unset_pixels(random.choice(self.access_tokens)[0])

            if len(targets) == 0:
                time.sleep(5)
            else:
                target = targets.pop(0)

                for index, name in id_names:
                    worker = workers[name]

                    if not worker["active"] or target is None:
                        continue

                    # reduce CPU usage
                    time.sleep(0.25)

                    # get the current time
                    current_timestamp = math.floor(time.time())

                    # log next time until drawing
                    time_until_next_draw = worker["next_draw_time"] - current_timestamp

                    if time_until_next_draw > 10000:
                        logger.warning(
                            "Worker #{} - {} :: CANCELLED :: Rate-Limit Banned", index, name
                        )
                        worker["active"] = False

                    self.refresh_token(index, name, worker)

                    # draw pixel onto screen
                    if self.access_tokens.get(index) is not None and (
                        current_timestamp >= worker["next_draw_time"]
                        or self.first_run_counter <= index
                    ):

                        # place pixel immediately
                        # first_run = False
                        self.first_run_counter += 1

                        # get target color
                        # target_rgb = pix[current_r, current_c]

                        # get current pixel position from input image and replacement color
                        current_r, current_c, new_rgb = target

                        # get converted color
                        new_rgb_hex = ColorMapper.rgb_to_hex(new_rgb)
                        pixel_color_index = ColorMapper.COLOR_MAP[new_rgb_hex]

                        logger.info("Account Placing: {}", name)

                        # draw the pixel onto r/place
                        # There's a better way to do this
                        pixel_x_start = self.pixel_x_start + current_r
                        pixel_y_start = self.pixel_y_start + current_c

                        # draw the pixel onto r/place
                        worker["next_draw_time"], success = self.set_pixel_and_check_ratelimit(
                            self.access_tokens[index][0],
                            pixel_x_start,
                            pixel_y_start,
                            name,
                            pixel_color_index,
                            index,
                        )

                        if success:
                            if len(targets) > 0:
                                target = targets.pop(0)
                            else:
                                target = None

    def start(self):
        threading.Thread(
            target=self.main_thread,
            args=[self.json_data["workers"]],
        ).start()


@click.command()
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable verbose mode. Prints debug messages to the console.",
)
@click.option(
    "-d",
    "--dry",
    is_flag=True,
    help="Enable dry mode. Don't actually place any pixels.",
)
@click.option(
    "-c",
    "--config",
    default="config.json",
    help="Location of config.json",
)
def main(verbose: bool, dry: bool, config: str):

    if not verbose:
        # default loguru level is DEBUG
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    client = PlaceClient(dry=dry, config_path=config)
    # Start everything
    client.start()


if __name__ == "__main__":
    main()
