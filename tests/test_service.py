import unittest
from axle_reconciliation import process


class AxleTests(unittest.TestCase):
    def base(self):
        return {"sections":[{"id":"s1","entry":"a","exit":"b"}],"events":[{"id":"e2","train_id":"t1","checkpoint":"b","time":"2026-01-01T00:02:00Z","axles":20},{"id":"e1","train_id":"t1","checkpoint":"a","time":"2026-01-01T00:00:00Z","axles":20}]}

    def test_orders_events_and_balances(self):
        row=process(self.base())["movements"][0]
        self.assertEqual((row["status"],row["duration_seconds"]),("balanced",120))

    def test_mismatch_is_preserved(self):
        data=self.base(); data["events"][0]["axles"]=19
        self.assertEqual(process(data)["movements"][0]["status"],"axle_mismatch")

    def test_exit_without_entry_is_anomaly(self):
        data=self.base(); data["events"]=data["events"][:1]
        self.assertEqual(process(data)["anomalies"][0]["type"],"exit_without_entry")

    def test_open_entry_is_occupied(self):
        data=self.base(); data["events"]=data["events"][1:]
        self.assertEqual(process(data)["occupied"][0]["train_id"],"t1")

    def test_duplicate_entry_is_anomaly(self):
        data=self.base(); data["events"].append({"id":"e3","train_id":"t1","checkpoint":"a","time":"2026-01-01T00:01:00Z","axles":20})
        self.assertEqual(process(data)["anomalies"][0]["type"],"duplicate_entry")

    def test_shared_checkpoint_rejected(self):
        data=self.base(); data["sections"].append({"id":"s2","entry":"b","exit":"c"})
        with self.assertRaises(ValueError): process(data)


if __name__=="__main__": unittest.main()

