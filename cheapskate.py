#!/usr/bin/env python3
import subprocess
import json
from datetime import datetime as dt, timedelta
from jsonpath_rw import parse
import uwsgi


class Instance:
    """
        Represents an ec2 instance with some helper functions
        to use a cheapskate tag to turn instances on and off.
    """
    GROUPS = {
        "0": "Default Off",
        "1": "Default On",
        "2": "Business Hours"
    }

    DATEFORMAT = '%Y-%m-%dT%H:%M'
    CHEAPSKATE = { "grp": "1", "user": "", "off": "", "req": "" }
    PRODUCTS = json.load(open("ec2prices.json"))
    COSTTHRESHOLD = 20
    BUSINESSHOURSTART = '06:30'
    BUSINESSHOUREND = '18:30'
    BUSINESSDAYSOFWEEK = range(0,5) # Monday - Friday (values are from 0-6 with monday being 0)

    def __init__(self, instance_data):
        self.instance_id = instance_data["InstanceId"]
        self.raw = instance_data
        self.cheapskate = Instance.CHEAPSKATE.copy()
        self.name = ""
        if "cheapskate" in [a["Key"] for a in self.raw["Tags"]]:
            cheapskate_raw = [a for a in self.raw["Tags"] if a["Key"] == "cheapskate"][0]["Value"].strip()
            if cheapskate_raw != "":
                self.cheapskate.update(dict([a.split("=") for a in cheapskate_raw.split("/")]))
        if "Name" in [a["Key"] for a in self.raw["Tags"]]:
            self.name = [a for a in self.raw["Tags"] if a["Key"] == "Name"][0]["Value"].strip()
        product_key = self.raw["InstanceType"] + "." + self.raw.get("Platform", "linux").title()
        self.product = Instance.PRODUCTS[product_key]
        pricedims = parse("terms.*.priceDimensions.*").find(self.product)
        if not pricedims:
            pricedims = parse("terms").find(self.product)
        pricedims = pricedims[0].value
        self.price = pricedims["pricePerUnit"]["USD"]
        self.product["terms"] = pricedims

    def save(self):
        cheapskate_raw = "/".join([key + "=" + value for key, value in self.cheapskate.items() if key in Instance.CHEAPSKATE.keys()])
        subprocess.check_output(["aws", "ec2", "create-tags", "--resources", self.instance_id, "--tags", 'Key=cheapskate,Value="{}"'.format(cheapskate_raw)])
        uwsgi.cache_del("raw_aws")

    @classmethod
    def objects(cls):
        if not uwsgi.cache_exists("raw_aws"):
            if hasattr(cls, "_objects"):
                del cls._objects
            uwsgi.cache_set("raw_aws", subprocess.check_output(["aws", "ec2", "describe-instances", "--no-paginate"]), 60*15)
        raw = json.loads(uwsgi.cache_get("raw_aws").decode("utf-8"))
        if hasattr(cls, "_objects"):
            return cls._objects
        objects = {}
        for data in raw["Reservations"]:
            for instance_data in data["Instances"]:
                instance = Instance(instance_data=instance_data)
                objects[instance.instance_id] = instance
        cls._objects = objects
        return objects  # A dict

    def as_dict(self):
        data = self.cheapskate
        data["name"] = self.name
        data["id"] = self.instance_id
        data["status"] = self.raw["State"]["Name"]
        data["type"] = self.raw["InstanceType"]
        data["launchtime"] = dt.strftime(dt.strptime(self.raw["LaunchTime"], Instance.DATEFORMAT + ":%S.%fZ"), Instance.DATEFORMAT)
        data["hourlycost"] = "%.3f"%float(self.price)
        data["product"] = self.product
        return data  # A dictionary?

    @classmethod
    def objects_list(cls):
        return [i.as_dict() for i in cls.objects().values()]

    @classmethod
    def save_all(cls):
        for instanceid, instance in cls.objects().items():
            instance.save()

    @classmethod
    def shutdown_due(cls, hours=0):
        results = {}
        for instanceid, instance in cls.objects().items():
            if instance.cheapskate["grp"] != "1" and instance.raw["State"]["Code"] == 16: # Code 16 is running
                try:
                    if dt.strptime(instance.cheapskate["off"], Instance.DATEFORMAT) < dt.now() + timedelta(hours=hours):
                        results[instanceid] = instance
                except ValueError:
                    pass
        return results

    @classmethod
    def start_business_hours(cls):
        dtstart = dt.strptime(Instance.BUSINESSHOURSTART, "%H:%M")
        dtend = dt.strptime(Instance.BUSINESSHOUREND, "%H:%M")
        hours = (dtend - dt.now()).seconds / 3600 

        if dt.today().weekday() not in Instance.BUSINESSDAYSOFWEEK:
            return False
        if dt.now().time() < dtstart.time():
            return False
        if dt.now().time() > dtend.time():
            return False

        results = []
        for instanceid, instance in cls.objects().items():
            if instance.cheapskate["grp"] != "2" or instance.raw["State"]["Code"] != 80: # Code 80 is stopped
                continue
            results.append(instanceid)
            instance.update(user="Cheapskate", hours=hours, sysstart=1)
        return results

    def update(self, user, hours, sysstart=0):
        # Ensure that hours is not shorter than the current time.
        reqtime = dt.now() + timedelta(hours=hours)
        try:
            if reqtime < dt.strptime(self.cheapskate["off"], Instance.DATEFORMAT):
                return False
        except ValueError:
            print ("invalid date format: {}".format(self.cheapskate["off"]))
        
        self.cheapskate["user"] = user
        self.cheapskate["req"] = dt.strftime(reqtime, Instance.DATEFORMAT)
        if sysstart == 0 and float(self.price) * int(hours) >= float(Instance.COSTTHRESHOLD):
            self.cheapskate["off"] = dt.strftime(dt.now() + timedelta(hours=(Instance.COSTTHRESHOLD/self.price)), Instance.DATEFORMAT)
        else:
            self.cheapskate["off"] = dt.strftime(reqtime, Instance.DATEFORMAT)
        self.start()
        self.save()
        return True

    def start(self):
        return subprocess.check_output(["aws", "ec2", "start-instances", "--instance-ids", self.instance_id])
    
    def shutdown(self):
        if self.cheapskate["grp"] == "1":
            return False
        elif self.cheapskate["grp"] == "0" or self.cheapskate["grp"] == "2":
            if dt.strptime(self.cheapskate["off"], Instance.DATEFORMAT) < dt.now():
                result = subprocess.check_output(["aws", "ec2", "stop-instances", "--instance-ids", self.instance_id]).decode('utf-8')
                self.save()
                return result
            else:
                return json.dumps({"offtime" : self.cheapskate["off"]})
        else:
            return None

    def tag_instance(self, tagName, tagValue):
        return subprocess.check_output(["aws", "ec2", "create-tags", "--resources", self.instance_id, "--tags", "Key={},Value={}".format(tagName,tagValue)])
        

    def update_volume_tags(self, tagName):
        volumes = json.loads(subprocess.check_output(["aws", "ec2", "describe-volumes", "--filters", "Name=attachment.instance-id,Values={}".format(self.instance_id)]))

        log = {}
        instanceTag = ""
        for tag in self.raw["Tags"]:
            instanceTag = tag["Value"] if (tag["Key"] == tagName) else ""

        for volume in volumes:
            if self.instance_id not in log[self.instance_id]:
                log[self.instance_id] = {}
            log[self.instance_id][volume["VolumeId"]] = subprocess.check_output(["aws", "ec2", "create-tags", "--resources", volume["VolumeId"], "--tags", "Key={},Value={}".format(tagName, instanceTag)])
            print "Tagged volumes"

            snapshots = json.loads(subprocess.check_output(["aws", "ec2", "describe-snapshots", "--filters", "Name=tag:Managed,Values=true", "Name=volume-id,Values={}".format(volume["VolumeId"])]))
            print "Tagged snapshots", len(snapshots["Snapshots"])
            log[self.instance_id][volume["VolumeId"]]["Snapshots"] = {}

            for snap in snapshots["Snapshots"]:
                log[self.instance_id][volume["VolumeId"]]["Snapshots"][snap["SnapshotId"]] = subprocess.check_output(["aws", "ec2", "create-tags", "--resources", snap["SnapshotId"], "--tags", "Key={},Value={}".format(tagName,tagValue)])

        return log

    def __str__(self):
        return "{} ({})".format(self.raw["InstanceId"], [a for a in self.raw["Tags"] if a["Key"] == "Name"][0]["Value"])

