import copy, os, random, sys, tempfile, time

sys.path.insert(0, os.path.join(os.getcwd(), "server"))
import castles
import players as pl

P = pl.Players(tempfile.mkdtemp(), "new", log=lambda *a: None)
castles.register_players(P)
fakes = castles.fake_player_castles()
rnd = random.Random(1)
for i in range(174):
    a = P.get(f"user{i}").state.account
    a["DisplayName"] = f"User{i}"
    a["TrophyScore"] = rnd.randint(0, 1500)
    a["BuildInfo"]["Level"] = rnd.randint(1, 30)
    if i % 2 == 0:
        a["PublishedRooms"] = copy.deepcopy(rnd.choice(fakes)["Rooms"])

for reroll in range(6):
    t = time.time()
    r = castles.attack_selection(10, [], 5, reroll, [], 2000001, {}, [])
    n = sum(len(g["Castles"]) for g in r["CastlesByLevel"])
    print(f"reroll {reroll}: {n} castles in {time.time() - t:.2f}s")
