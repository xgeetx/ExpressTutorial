var mongoose = require('mongoose');

var Schema = mongoose.Schema;

var GenreSchema = new Schema(
    {
        name: {type: String, require: true, min:3, max:100}
    }
);

// Virtual for the genre's URL, named url
GenreSchema
.virtual('url')
.get(function () {
    return '/catalog/genre' + this._id;
});

module.exports = mongoose.model('Genre', GenreSchema);