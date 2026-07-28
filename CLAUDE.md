my goal is to create a robust dataset for the top 200 players in fantasy football for 2025. It should be game level data for the entire career of each player. Eventually, I want to use this data to create a model. 

I've found I can get player stats this way:
python
```
pd.read_html('https://www.espn.com/nfl/player/gamelog/_/id/4242335/type/nfl/year/2024')
```
But it requires me to know each player code. However, ESPN does have an api endpoint we could use to find player codes. Here is the url for that API: https://sports.core.api.espn.com/v3/sports/football/nfl/athletes?limit=20000

part of the fun in this is that I get to create my own data source. Even if some python package out there exists, I want to create this. 

I want to build this iteratively, so lets start with building out the dataset for Jonathan Taylor only, then we'll progress to other players and eventually a full list. 

Python is the language of choice to build this out. 